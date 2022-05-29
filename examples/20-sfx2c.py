from pyscf import gto, dh


mol = gto.M(atom=
'''
O  .0  .0     .0
H  .0  -0.757 0.587
H  .0  0.757  0.587
'''
, basis='def2-svp', charge=1, spin=1,
verbose=4).build()
mf = dh.DFDH(mol, xc='XYG3').x2c().kernel()

