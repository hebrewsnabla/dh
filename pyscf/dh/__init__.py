from dh.rdfdh import RDFDH


# TODO LIST: auxiliary integral transformation
# TODO (0) determine shape of cderi: (n, n, a) or (a, n, n)
# TODO (1) write multiple interface of cderi in MO representation
# TODO (1) benchmark speed of utilizing pyscf's outcore+nr_e2
# TODO (3) handle nmo != nao
# TODO (3) handle int2c2e linear dependent
# TODO (3) handle int2c2e eig situation for nuclear coordinate derivative

# TODO LIST: future works
# TODO RIJONX
# TODO RIJCOSX (sgx)
# TODO near-canonical U matrix