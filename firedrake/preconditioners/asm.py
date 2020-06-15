import abc

from pyop2.mpi import COMM_SELF
from firedrake.preconditioners.base import PCBase
from firedrake.petsc import PETSc
from firedrake.dmhooks import get_function_space

__all__ = ("ASMPatchPC", "ASMLinesmoothPC")


class ASMPatchPC(PCBase):
    ''' PC for PETSc PCASM

    should implement:
    - :meth:`get_patches`
    '''

    @abc.abstractproperty
    _prefix = "pc_asm_"

    def initialize(self, pc):
        # Get context from pc
        _, P = pc.getOperators()
        dm = pc.getDM()
        self.prefix = pc.getOptionsPrefix() + self._prefix

        # Extract function space and mesh to obtain plex and indexing functions
        V = get_function_space(dm)

        # Obtain patches from user defined funtion
        ises = self.get_patches(V)

        # Create new PC object as ASM type and set index sets for patches
        asmpc = PETSc.PC().create(comm=pc.comm)
        asmpc.incrementTabLevel(1, parent=pc)
        asmpc.setOptionsPrefix(self.prefix + "_sub")
        asmpc.setOperators(*pc.getOperators())
        asmpc.setType(asmpc.Type.ASM)
        asmpc.setASMLocalSubdomains(len(ises), ises)
        asmpc.setFromOptions()
        self.asmpc = asmpc

    @abc.abstractmethod
    def get_patches(self, V):
        ''' Get the patches used for PETSc PSASM
        '''
        pass

    def update(self, pc):
        pass

    def apply(self, pc, x, y):
        self.asmpc.apply(x, y)

    def applyTranspose(self, pc, x, y):
        self.asmpc.applyTranspose(x, y)


class ASMLinesmoothPC(ASMPatchPC):
    '''Linesmoother PC for extruded meshes implemented as an ASMPatchPC.

    These patches are made by looping over base mesh entities of a
    particular codimension, and collecting all the DOFs on the
    "horizontal" faces.  Then looping over all facet DOFs and
    collecting the DOFs on the "vertical" faces.

    Set the codimension of the base mesh in PETSc options via 'pc_linesmooth_codims'.
    '''

    _prefix = "pc_linesmooth_"
    
    def get_patches(self, V):
        mesh = V._mesh
        assert mesh.cell_set._extruded
        dm = mesh._plex
        section = V.dm.getDefaultSection()
        lgmap = V.dof_dset.lgmap

        # Obtain the codimensions to loop over from options, if present
        codim_list = PETSc.Options().getString(self.prefix+"codims", "0, 1")
        codim_list = [int(ii) for ii in codim_list.split(",")]

        # Build index sets for the patches
        ises = []
        # Loop over faces of base mesh, then edges
        for codim in codim_list:
            for f in range(*dm.getHeightStratum(codim)):
                # Only want to build patches over owned faces
                if dm.getLabelValue("pyop2_ghost", f) != -1:
                    continue
                # Collect all dofs that plex thinks live on the face or edge
                dof = section.getDof(f)
                if dof <= 0:
                    continue
                off = section.getOffset(f)
                indices = range(off, off+dof)
                # Map local indices into global indices and create the IS for PCASM
                global_index = lgmap.apply(indices)
                iset = PETSc.IS().createGeneral(global_index, comm=COMM_SELF)
                ises.append(iset)

        return ises
