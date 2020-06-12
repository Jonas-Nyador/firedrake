from functools import partial
from itertools import chain
import numpy as np

from ufl import MixedElement, VectorElement, TensorElement, replace

from pyop2 import op2
import loopy

from firedrake.petsc import PETSc
from firedrake.preconditioners.base import PCBase
from firedrake.dmhooks import attach_hooks, get_appctx, push_appctx, pop_appctx
from firedrake.dmhooks import add_hook, get_parent, push_parent, pop_parent
from firedrake.dmhooks import get_function_space, set_function_space
from firedrake.solving_utils import _SNESContext
import firedrake


class PMGPC(PCBase):
    """
    A class for implementing p-multigrid.

    Internally, this sets up a DM with a custom coarsen routine
    that p-coarsens the problem. This DM is passed to an internal
    PETSc PC of type MG and with options prefix 'pmg_'. The
    relaxation to apply on every p-level is described by 'pmg_mg_levels_',
    and the coarse solve by 'pmg_mg_coarse_'. Geometric multigrid
    or any other solver in firedrake may be applied to the coarse problem.
    An example chaining p-MG, GMG and AMG is given in the tests.

    The p-coarsening is implemented in the `coarsen_element` routine.
    This takes in a :class:`ufl.FiniteElement` and either returns a
    new, coarser element, or raises a `ValueError` (if the supplied element
    should be the coarsest one of the hierarchy).

    The default coarsen_element is to perform power-of-2 reduction
    of the polynomial degree. For mixed systems a `NotImplementedError`
    is raised, as I don't know how to make a sensible default for this.
    It is expected that many (most?) applications of this preconditioner
    will subclass :class:`PMGPC` to override `coarsen_element`.
    """
    @staticmethod
    def coarsen_element(ele):
        """
        Coarsen a given element to form the next problem down in the p-hierarchy.

        If the supplied element should form the coarsest level of the p-hierarchy,
        raise `ValueError`. Otherwise, return a new :class:`ufl.FiniteElement`.

        By default, this does power-of-2 coarsening in polynomial degree.
        It raises a `NotImplementedError` for :class:`ufl.MixedElement`s, as
        I don't know if there's a sensible default strategy to implement here.
        It is intended that the user subclass `PMGPC` to override this method
        for their problem.

        :arg ele: a :class:`ufl.FiniteElement` to coarsen.
        """
        if isinstance(ele, MixedElement) and not isinstance(ele, (VectorElement, TensorElement)):
            raise NotImplementedError("Implement this method yourself")

        degree = ele.degree()
        family = ele.family()

        if family == "Discontinuous Galerkin" and degree == 0:
            raise ValueError
        elif degree == 1:
            raise ValueError

        return ele.reconstruct(degree=degree // 2)

    def initialize(self, pc):
        # Make a new DM.
        # Hook up a (new) coarsen routine on that DM.
        # Make a new PC, of type MG.
        # Assign the DM to that PC.

        odm = pc.getDM()
        ctx = get_appctx(odm)

        test, trial = ctx.J.arguments()
        if test.function_space() != trial.function_space():
            raise NotImplementedError("test and trial spaces must be the same")

        # Construct a list with the elements we'll be using
        V = test.function_space()
        ele = V.ufl_element()
        elements = [ele]
        while True:
            try:
                ele = self.coarsen_element(ele)
            except ValueError:
                break
            elements.append(ele)

        pdm = PETSc.DMShell().create(comm=pc.comm)
        sf = odm.getPointSF()
        section = odm.getDefaultSection()
        attach_hooks(pdm, level=len(elements)-1, sf=sf, section=section)
        # Now overwrite some routines on the DM
        pdm.setRefine(None)
        pdm.setCoarsen(self.coarsen)
        pdm.setCreateInterpolation(self.create_interpolation)
        set_function_space(pdm, get_function_space(odm))

        parent = get_parent(odm)
        assert parent is not None
        add_hook(parent, setup=partial(push_parent, pdm, parent), teardown=partial(pop_parent, pdm, parent),
                 call_setup=True)
        add_hook(parent, setup=partial(push_appctx, pdm, ctx), teardown=partial(pop_appctx, pdm, ctx),
                 call_setup=True)

        ppc = PETSc.PC().create(comm=pc.comm)
        ppc.setOptionsPrefix(pc.getOptionsPrefix() + "pmg_")
        ppc.setType("mg")
        ppc.setOperators(*pc.getOperators())
        ppc.setDM(pdm)
        ppc.incrementTabLevel(1, parent=pc)

        # PETSc unfortunately requires us to make an ugly hack.
        # We would like to use GMG for the coarse solve, at least
        # sometimes. But PETSc will use this p-DM's getRefineLevels()
        # instead of the getRefineLevels() of the MeshHierarchy to
        # decide how many levels it should use for PCMG applied to
        # the p-MG's coarse problem. So we need to set an option
        # for the user, if they haven't already; I don't know any
        # other way to get PETSc to know this at the right time.
        opts = PETSc.Options(pc.getOptionsPrefix() + "pmg_")
        if "mg_coarse_pc_mg_levels" not in opts:
            opts["mg_coarse_pc_mg_levels"] = odm.getRefineLevel() + 1

        ppc.setFromOptions()
        ppc.setUp()
        self.ppc = ppc

    def apply(self, pc, x, y):
        return self.ppc.apply(x, y)

    def applyTranspose(self, pc, x, y):
        return self.ppc.applyTranspose(x, y)

    def update(self, pc):
        pass

    def coarsen(self, fdm, comm):
        fctx = get_appctx(fdm)
        test, trial = fctx.J.arguments()
        fV = test.function_space()
        fu = fctx._problem.u

        cele = self.coarsen_element(fV.ufl_element())
        cV = firedrake.FunctionSpace(fV.mesh(), cele)
        cdm = cV.dm
        cu = firedrake.Function(cV)

        parent = get_parent(fdm)
        assert parent is not None
        add_hook(parent, setup=partial(push_parent, cdm, parent), teardown=partial(pop_parent, cdm, parent),
                 call_setup=True)

        replace_d = {fu: cu,
                     test: firedrake.TestFunction(cV),
                     trial: firedrake.TrialFunction(cV)}
        cJ = replace(fctx.J, replace_d)
        cF = replace(fctx.F, replace_d)
        if fctx.Jp is not None:
            cJp = replace(fctx.Jp, replace_d)
        else:
            cJp = None

        cbcs = []
        for bc in fctx._problem.bcs:
            # Don't actually need the value, since it's only used for
            # killing parts of the matrix. This should be generalised
            # for p-FAS, if anyone ever wants to do that

            cV_ = cV
            for index in bc._indices:
                cV_ = cV_.sub(index)

            cbcs.append(firedrake.DirichletBC(cV_, firedrake.zero(cV_.shape),
                                              bc.sub_domain,
                                              method=bc.method))

        fcp = fctx._problem.form_compiler_parameters
        cproblem = firedrake.NonlinearVariationalProblem(cF, cu, cbcs, cJ,
                                                         Jp=cJp,
                                                         form_compiler_parameters=fcp,
                                                         is_linear=fctx._problem.is_linear)

        cctx = _SNESContext(cproblem, fctx.mat_type, fctx.pmat_type,
                            appctx=fctx.appctx,
                            pre_jacobian_callback=fctx._pre_jacobian_callback,
                            pre_function_callback=fctx._pre_function_callback,
                            post_jacobian_callback=fctx._post_jacobian_callback,
                            post_function_callback=fctx._post_function_callback,
                            options_prefix=fctx.options_prefix,
                            transfer_manager=fctx.transfer_manager)

        add_hook(parent, setup=partial(push_appctx, cdm, cctx), teardown=partial(pop_appctx, cdm, cctx),
                 call_setup=True)

        cdm.setKSPComputeOperators(_SNESContext.compute_operators)
        cdm.setCreateInterpolation(self.create_interpolation)

        # If we're the coarsest grid of the p-hierarchy, don't
        # overwrite the coarsen routine; this is so that you can
        # use geometric multigrid for the p-coarse problem
        try:
            self.coarsen_element(cele)
            cdm.setCoarsen(self.coarsen)
        except ValueError:
            pass

        return cdm

    def create_interpolation(self, dmc, dmf):
        # This should be generalised to work for arbitrary function
        # spaces. Currently I think it only works for CG/DG on simplices.
        # I used the same code as firedrake.P1PC.
        cctx = get_appctx(dmc)
        fctx = get_appctx(dmf)

        cV = cctx.J.arguments()[0].function_space()
        fV = fctx.J.arguments()[0].function_space()

        cbcs = cctx._problem.bcs
        fbcs = fctx._problem.bcs

        I = prolongation_matrix(fV, cV, fbcs, cbcs)
        R = PETSc.Mat().createTranspose(I)
        return R, None

    def view(self, pc, viewer=None):
        if viewer is None:
            viewer = PETSc.Viewer.STDOUT
        viewer.printfASCII("p-multigrid PC\n")
        self.ppc.view(viewer)


def prolongation_transfer_kernel_full(Pk, P1):
    # Works for Pk, Pm; I just retain the notation
    # P1 to remind you that P1 is of lower degree
    # than Pk
    from tsfc import compile_expression_dual_evaluation
    from tsfc.fiatinterface import create_element
    from firedrake import TestFunction

    expr = TestFunction(P1)
    coords = Pk.ufl_domain().coordinates
    to_element = create_element(Pk.ufl_element(), vector_is_mixed=False)

    ast, oriented, needs_cell_sizes, coefficients, _ = compile_expression_dual_evaluation(expr, to_element, coords, coffee=False)
    kernel = op2.Kernel(ast, ast.name)
    return kernel

def isTensorProductSpace(V):
    ele = V.ufl_element()
    cell = ele.cell()
    isquad = cell.cellname().find("quadrilateral")>=0
    
    if(isinstance(ele, (firedrake.VectorElement, firedrake.TensorElement))):
        subel = ele.sub_elements()
        ele = subel[0]

    N = ele.degree()
    if(isinstance(ele, firedrake.TensorProductElement)):
        subel = ele.sub_elements()
        ele = subel[0]
        N = N[0]
    
    family = ele.family()
    tpfamily = set(["Q", "CG", "Lagrange", "DQ", "DG", "Discontinuous Lagrange"])
    isquad = isquad and (family in tpfamily)
    return isquad, N, family


class StandaloneInterpolationMatrix(object):
    """
    Interpolation matrix for a single standalone space.
    """
    def __init__(self, Vf, Vc, Vf_bcs, Vc_bcs):
        self.Vf = Vf
        self.Vc = Vc
        self.Vf_bcs = Vf_bcs
        self.Vc_bcs = Vc_bcs

        self.uc = firedrake.Function(Vc)
        self.uf = firedrake.Function(Vf)
        
        self.mesh = Vf.mesh()
        self.weight = self.multiplicity(Vf)
        with self.weight.dat.vec as w:
            w.reciprocal()

        tf,_,_=isTensorProductSpace(Vf) 
        tc,_,_=isTensorProductSpace(Vc) 
        if(tf and tc):
            self.make_blas_kernels(Vf,Vc)
        else:
            self.make_kernels(Vf,Vc)
        return

    def make_kernels(self,Vf,Vc):
        """
        The way we transpose the prolongation kernel is suboptimal, but works for general elements.
        A local matrix has to be generated each time the kernel is executed.
        We are waiting for structure-preserving tfsc kernels. 
        As an alternative we provide blas kernels for tensor product elements.
        """
        self.prolong_kernel = self.prolongation_transfer_kernel_action(Vf, self.uc)
        matrix_kernel = self.prolongation_transfer_kernel_action(Vf, firedrake.TestFunction(Vc))
        element_kernel = loopy.generate_code_v2(matrix_kernel.code).device_code()
        element_kernel = element_kernel.replace("void expression_kernel", "static void expression_kernel")
        dimc = Vc.finat_element.space_dimension() * Vc.value_size
        dimf = Vf.finat_element.space_dimension() * Vf.value_size
        restrict_code = f"""
        {element_kernel}

        void restriction(double *restrict Rc, const double *restrict Rf, const double *restrict w)
        {{
            double Afc[{dimf}*{dimc}] = {{0}};
            expression_kernel(Afc);
            for (int32_t i = 0; i < {dimf}; i++)
               for (int32_t j = 0; j < {dimc}; j++)
                   Rc[j] += Afc[i*{dimc} + j] * Rf[i] * w[i];
        }}
        """
        self.restrict_kernel = op2.Kernel(restrict_code, "restriction")
        return
   

    @staticmethod
    def prolongation_transfer_kernel_action(Vf, expr):
        from tsfc import compile_expression_dual_evaluation
        from tsfc.fiatinterface import create_element
        coords = Vf.ufl_domain().coordinates
        to_element = create_element(Vf.ufl_element(), vector_is_mixed=False)
        ast, oriented, needs_cell_sizes, coefficients, _ = compile_expression_dual_evaluation(expr, to_element, coords, coffee=False)
        return op2.Kernel(ast, ast.name)


    def make_blas_kernels(self,Vf,Vc):
        ndim = Vf.ufl_domain().topological_dimension()
        zf = self.get_nodes_1d(Vf)
        zc = self.get_nodes_1d(Vc)
        Jnp = self.barycentric(zc, zf)
        (mx,nx) = Jnp.shape
        (my,ny) = (mx,nx)
        (mz,nz) = (mx,nx) if(ndim==3) else (1,1)
        nscal = np.prod(Vf.shape,dtype=int)
        mxyz = mx*my*mz 
        nxyz = nx*ny*nz
        lwork = nscal*mxyz

        Jhat = np.ascontiguousarray( Jnp.T )
        JX = ', '.join(map(float.hex, np.matrix.flatten(Jhat)))
        JZ = "JX" if(ndim==3) else "&one"

        kronmxv_code = """
        extern void dgemm_(char *TRANSA, char *TRANSB, int *m, int *n, int *k,
              double *alpha, double *A, int *lda, double *B, int *ldb,
              double *beta , double *C, int *ldc);

        void kronmxv(int tflag, 
            int mx, int my, int mz,
            int nx, int ny, int nz, int nel,
            double *A1, double *A2, double *A3, 
            double *x , double *y){

        int m,n,k,s,p,lda;
        char TA1, TA2, TA3;
        char tran='T', notr='N';
        double zero=0.0E0, one=1.0E0;

        if(tflag>0){
           TA1 = tran;
           TA2 = notr;
        }else{
           TA1 = notr;
           TA2 = tran;
        }
        TA3 = TA2;

        m = mx;  k = nx;  n = ny*nz*nel;
        lda = (tflag>0)? nx : mx;
        dgemm_(&TA1, &notr, &m,&n,&k, &one, A1,&lda, x,&k, &zero, y,&m);

        p = 0;  s = 0;
        m = mx;  k = ny;  n = my;
        lda = (tflag>0)? ny : my;
        for(int i=0; i<nz*nel; i++){
           dgemm_(&notr, &TA2, &m,&n,&k, &one, y+p,&m, A2,&lda, &zero, x+s,&m);
           p += m*k;
           s += m*n;
        }

        p = 0;  s = 0;
        m = mx*my;  k = nz;  n = mz;
        lda = (tflag>0)? nz : mz;
        for(int i=0; i<nel; i++){
           dgemm_(&notr, &TA3, &m,&n,&k, &one, x+p,&m, A3,&lda, &zero, y+s,&m);
           p += m*k;
           s += m*n;
        }
        return;
        }
        """

        prolong_code = f"""
        {kronmxv_code}

        void prolongation(double *restrict y, const double *restrict x){{
            double JX[{mx}*{nx}] = {{ {JX} }};
            double t0[{lwork}], t1[{lwork}];
            double one=1.0E0;

            for(int j=0; j<{nxyz}; j++)
                for(int i=0; i<{nscal}; i++)
                    t0[j + {nxyz}*i] = x[i + {nscal}*j];

            kronmxv(0, {mx},{my},{mz}, {nx},{ny},{nz}, {nscal}, JX,JX,{JZ}, t0,t1);

            for(int j=0; j<{mxyz}; j++)
                for(int i=0; i<{nscal}; i++)
                   y[i + {nscal}*j] = t1[j + {mxyz}*i];
            return;
        }}
        """

        restrict_code = f"""
        {kronmxv_code}

        void restriction(double *restrict y, const double *restrict x, 
            const double *restrict w){{
            double JX[{mx}*{nx}] = {{ {JX} }};
            double t0[{lwork}], t1[{lwork}];
            double one=1.0E0;

            for(int j=0; j<{mxyz}; j++)
                for(int i=0; i<{nscal}; i++)
                    t0[j + {mxyz}*i] = x[i + {nscal}*j] * w[i + {nscal}*j];

            kronmxv(1, {nx},{ny},{nz}, {mx},{my},{mz}, {nscal}, JX,JX,{JZ}, t0,t1);
            
            for(int j=0; j<{nxyz}; j++)
                for(int i=0; i<{nscal}; i++)
                    y[i + {nscal}*j] += t1[j + {nxyz}*i];
            return;
        }}
        """
        op2.configuration["ldflags"] = "-lblas"
        self.prolong_kernel = op2.Kernel(prolong_code, "prolongation")
        self.restrict_kernel = op2.Kernel(restrict_code, "restriction")
        return
    

    @staticmethod
    def get_nodes_1d(V):
        from FIAT import quadrature
        from FIAT.reference_element import DefaultLine
        
        isquad, N, family = isTensorProductSpace(V)
        assert isquad
        if(family=="Q" or family=="CG" or family=="Lagrange"):
            rule = quadrature.GaussLobattoLegendreQuadratureLineRule(DefaultLine(),N+1)
        elif(family=="DQ" or family=="DG" or family=="Discontinuous Lagrange"):
            rule = quadrature.GaussLegendreQuadratureLineRule(DefaultLine(),N+1)
        else:
            raise ValueError("Don't know how to get nodes for %r" % family)

        return np.matrix.flatten( rule.get_points() )


    @staticmethod
    def barycentric(xsrc,xdst):
        temp = np.subtract.outer(xsrc,xsrc)
        np.fill_diagonal(temp, 1.0E0)
        lam = 1.0E0 / np.prod(temp,axis=1)
        J = np.subtract.outer(xdst, xsrc)
        idx = np.argwhere(np.isclose(J,0.0E0,1E-14))
        J[idx[:,0],idx[:,1]] = 1.0E0
        J = lam/J
        J[idx[:,0],:] = 0.0E0
        J[idx[:,0],idx[:,1]] = 1.0E0
        J *= (1/np.sum(J,axis=1))[:,None]
        return J


    @staticmethod
    def multiplicity(V):
        # Lawrence's magic code for calculating dof multiplicities
        shapes = (V.finat_element.space_dimension(),
                  np.prod(V.shape))
        domain = "{[i,j]: 0 <= i < %d and 0 <= j < %d}" % shapes
        instructions = """
        for i, j
            w[i,j] = w[i,j] + 1
        end
        """
        weight = firedrake.Function(V)
        firedrake.par_loop((domain, instructions), firedrake.dx, {"w": (weight, op2.INC)},
                 is_loopy_kernel=True)
        return weight



    def mult(self, mat, resf, resc):
        """
        Implement restriction: restrict residual on fine grid resf to coarse grid resc.
        """

        with self.uf.dat.vec_wo as xf:
            resf.copy(xf)

        with self.uc.dat.vec_wo as xc:
            xc.set(0)

        [bc.zero(self.uf) for bc in self.Vf_bcs]

        op2.par_loop(self.restrict_kernel, self.mesh.cell_set,
                     self.uc.dat(op2.INC, self.uc.cell_node_map()),
                     self.uf.dat(op2.READ, self.uf.cell_node_map()),
                     self.weight.dat(op2.READ, self.weight.cell_node_map()))

        [bc.zero(self.uc) for bc in self.Vc_bcs]

        with self.uc.dat.vec_ro as xc:
            xc.copy(resc)

    def multTranspose(self, mat, xc, xf, inc=False):
        """
        Implement prolongation: prolong correction on coarse grid xc to fine grid xf.
        """

        with self.uc.dat.vec_wo as xc_:
            xc.copy(xc_)

        [bc.zero(self.uc) for bc in self.Vc_bcs]

        op2.par_loop(self.prolong_kernel, self.mesh.cell_set,
                     self.uf.dat(op2.WRITE, self.Vf.cell_node_map()),
                     self.uc.dat(op2.READ, self.Vc.cell_node_map()))

        [bc.zero(self.uf) for bc in self.Vf_bcs]

        with self.uf.dat.vec_ro as xf_:
            if inc:
                xf.axpy(1.0, xf_)
            else:
                xf_.copy(xf)

    def multTransposeAdd(self, mat, x, y, w):
        if y.handle == w.handle:
            self.multTranspose(mat, x, w, inc=True)
        else:
            self.multTranspose(mat, x, w)
            w.axpy(1.0, y)


class MixedInterpolationMatrix(object):
    """
    Interpolation matrix for a mixed finite element space.
    """
    def __init__(self, Vf, Vc, Vf_bcs, Vc_bcs):
        self.Vf = Vf
        self.Vc = Vc
        self.Vf_bcs = Vf_bcs
        self.Vc_bcs = Vc_bcs

        self.standalones = []
        for (i, (Vf_sub, Vc_sub)) in enumerate(zip(Vf, Vc)):
            Vf_sub_bcs = [bc for bc in Vf_bcs if bc.function_space().index == i]
            Vc_sub_bcs = [bc for bc in Vc_bcs if bc.function_space().index == i]
            standalone = StandaloneInterpolationMatrix(Vf_sub, Vc_sub, Vf_sub_bcs, Vc_sub_bcs)
            self.standalones.append(standalone)

        self.uc = firedrake.Function(Vc)
        self.uf = firedrake.Function(Vf)
        self.mesh = Vf.mesh()

    def mult(self, mat, resf, resc):

        with self.uf.dat.vec_wo as xf:
            resf.copy(xf)

        with self.uc.dat.vec_wo as xc:
            xc.set(0)

        for (i, standalone) in enumerate(self.standalones):
            op2.par_loop(standalone.restrict_kernel, standalone.mesh.cell_set,
                         self.uc.split()[i].dat(op2.INC, standalone.Vc.cell_node_map()),
                         self.uf.split()[i].dat(op2.READ, standalone.Vf.cell_node_map()),
                         standalone.weight.dat(op2.READ, standalone.weight.cell_node_map()))

        [bc.zero(self.uc) for bc in self.Vc_bcs]

        with self.uc.dat.vec_ro as xc:
            xc.copy(resc)

    def multTranspose(self, mat, xc, xf, inc=False):

        with self.uc.dat.vec_wo as xc_:
            xc.copy(xc_)

        [bc.zero(self.uc) for bc in self.Vc_bcs]

        for (i, standalone) in enumerate(self.standalones):
            op2.par_loop(standalone.prolong_kernel, standalone.mesh.cell_set,
                         self.uf.split()[i].dat(op2.WRITE, standalone.Vf.cell_node_map()),
                         self.uc.split()[i].dat(op2.READ, standalone.Vc.cell_node_map()))

        with self.uf.dat.vec_ro as xf_:
            if inc:
                xf.axpy(1.0, xf_)
            else:
                xf_.copy(xf)

    def multTransposeAdd(self, mat, x, y, w):
        if y.handle == w.handle:
            self.multTranspose(mat, x, w, inc=True)
        else:
            self.multTranspose(mat, x, w)
            w.axpy(1.0, y)







def prolongation_matrix_full(Pk, P1, Pk_bcs, P1_bcs):
    sp = op2.Sparsity((Pk.dof_dset,
                       P1.dof_dset),
                      (Pk.cell_node_map(),
                       P1.cell_node_map()))
    mat = op2.Mat(sp, PETSc.ScalarType)
    mesh = Pk.ufl_domain()

    fele = Pk.ufl_element()
    if isinstance(fele, MixedElement) and not isinstance(fele, (VectorElement, TensorElement)):
        for i in range(fele.num_sub_elements()):
            Pk_bcs_i = [bc for bc in Pk_bcs if bc.function_space().index == i]
            P1_bcs_i = [bc for bc in P1_bcs if bc.function_space().index == i]

            rlgmap, clgmap = mat[i, i].local_to_global_maps
            rlgmap = Pk.sub(i).local_to_global_map(Pk_bcs_i, lgmap=rlgmap)
            clgmap = P1.sub(i).local_to_global_map(P1_bcs_i, lgmap=clgmap)
            unroll = any(bc.function_space().component is not None
                         for bc in chain(Pk_bcs_i, P1_bcs_i) if bc is not None)
            matarg = mat[i, i](op2.WRITE, (Pk.sub(i).cell_node_map(), P1.sub(i).cell_node_map()),
                               lgmaps=(rlgmap, clgmap), unroll_map=unroll)
            op2.par_loop(prolongation_transfer_kernel_full(Pk.sub(i), P1.sub(i)), mesh.cell_set,
                         matarg)

    else:
        rlgmap, clgmap = mat.local_to_global_maps
        rlgmap = Pk.local_to_global_map(Pk_bcs, lgmap=rlgmap)
        clgmap = P1.local_to_global_map(P1_bcs, lgmap=clgmap)
        unroll = any(bc.function_space().component is not None
                     for bc in chain(Pk_bcs, P1_bcs) if bc is not None)
        matarg = mat(op2.WRITE, (Pk.cell_node_map(), P1.cell_node_map()),
                     lgmaps=(rlgmap, clgmap), unroll_map=unroll)
        op2.par_loop(prolongation_transfer_kernel_full(Pk, P1), mesh.cell_set,
                     matarg)

    mat.assemble()
    return mat.handle


def prolongation_matrix_matfree(Vf, Vc, Vf_bcs, Vc_bcs):
    fele = Vf.ufl_element()
    if isinstance(fele, MixedElement) and not isinstance(fele, (VectorElement, TensorElement)):
        ctx = MixedInterpolationMatrix(Vf, Vc, Vf_bcs, Vc_bcs)
    else:
        ctx = StandaloneInterpolationMatrix(Vf, Vc, Vf_bcs, Vc_bcs)

    sizes = (Vc.dof_dset.layout_vec.getSizes(), Vf.dof_dset.layout_vec.getSizes())
    M_shll = PETSc.Mat().createPython(sizes, ctx)
    M_shll.setUp()

    return M_shll


prolongation_matrix = prolongation_matrix_matfree
