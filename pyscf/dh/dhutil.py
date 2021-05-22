import os
import pickle
import shutil

import h5py
from pyscf import lib
from pyscf.ao2mo.outcore import balance_partition

import numpy as np
import tempfile
from time import time, process_time
from functools import wraps, partial


# To avoid too slow single-threaded einsum if pyscf-tblis is not available

lib.numpy_helper._numpy_einsum = partial(np.einsum, optimize=True)
# Doubly hybrid functionals xc code in detail
XC_DH_MAP = {   # [xc_s, xc_n, cc, c_os, c_ss]
    "mp2": ("HF", None, 1, 1, 1),
    "xyg3": ("B3LYPg", "0.8033*HF - 0.0140*LDA + 0.2107*B88, 0.6789*LYP", 0.3211, 1, 1),
    "xygjos": ("B3LYPg", "0.7731*HF + 0.2269*LDA, 0.2309*VWN3 + 0.2754*LYP", 0.4364, 1, 0),
    "xdhpbe0": ("PBE0", "0.8335*HF + 0.1665*PBE, 0.5292*PBE", 0.5428, 1, 0),
    "b2plyp": ("0.53*HF + 0.47*B88, 0.73*LYP", None, 0.27, 1, 1),
    "mpw2plyp": ("0.55*HF + 0.45*mPW91, 0.75*LYP", None, 0.25, 1, 1),
    "pbe0dh": ("0.5*HF + 0.5*PBE, 0.875*PBE", None, 0.125, 1, 1),
    "pbeqidh": ("0.693361*HF + 0.306639*PBE, 0.666667*PBE", None, 0.333333, 1, 1),
    "pbe02": ("0.793701*HF + 0.206299*PBE, 0.5*PBE", None, 0.5, 1, 1),
}


class TicToc:

    def __init__(self):
        self.t = time()
        self.p = process_time()

    def tic(self):
        self.t = time()
        self.p = process_time()

    def toc(self, msg=""):
        t = time() - self.t
        p = process_time() - self.p
        print("Wall: {:12.4f}, CPU: {:12.4f}, Ratio: {:6.1f}, msg: {:}".format(t, p, p / t * 100, msg))
        self.tic()


class HybridDict(dict):
    """
    HybridDict: Inherited dictionary class

    A dictionary specialized to store data both in memory and in disk.
    """
    def __init__(self, chkfile_name=None, dir=None, **kwargs):
        super(HybridDict, self).__init__(**kwargs)
        # initialize input variables
        if dir is None:
            dir = lib.param.TMPDIR
        if chkfile_name is None:
            self._chkfile = tempfile.NamedTemporaryFile(dir=dir)
            chkfile_name = self._chkfile.name
        # create or open exist chkfile
        self.chkfile_name = chkfile_name
        self.chkfile = h5py.File(self.chkfile_name, "r+")

    def create(self, name, data=None, incore=True, shape=None, dtype=None, **kwargs):
        if name in self:
            try:  # don't create a new space if tensor already exists
                if self[name].shape == shape:
                    self[name][:] = 0
                    return self.get(name)
            except (ValueError, AttributeError):
                # ValueError -- in h5py.h5d.create: Unable to create dataset (name already exists)
                # AttributeError -- [certain other type] object has no attribute 'shape'
                self.delete(name)
        dtype = dtype if dtype is not None else np.float64
        if not incore:
            self.chkfile.create_dataset(name, shape=shape, dtype=dtype, data=data, **kwargs)
            self.setdefault(name, self.chkfile[name])
        elif data is not None:
            self.setdefault(name, data)
        elif data is None and shape is not None:
            self.setdefault(name, np.zeros(shape=shape, dtype=dtype))
        else:
            raise ValueError("Could not handle create!")
        return self.get(name)

    def delete(self, key):
        val = self.pop(key)
        if isinstance(val, h5py.Dataset):
            try:
                del self.chkfile[key]
            except KeyError:  # h5py.h5g.GroupID.unlink: Couldn't delete link
                # another key maps to the same h5py dataset value, and this value has been deleted
                pass

    def load(self, key):
        return np.asarray(self.get(key))

    def dump(self, h5_path="tensors.h5", dat_path="tensors.dat"):
        dct = {}
        for key, val in self.items():
            if not isinstance(val, h5py.Dataset):
                dct[key] = val
        with open(dat_path, "wb") as f:
            pickle.dump(dct, f)
        self.chkfile.close()
        shutil.copy(self.chkfile_name, h5_path)
        self.chkfile = h5py.File(self.chkfile_name, "r+")
        # re-update keys stored on disk
        for key in HybridDict.get_dataset_keys(self.chkfile):
            self[key] = self.chkfile[key]

    @staticmethod
    def get_dataset_keys(f):
        # get h5py dataset keys to the bottom level https://stackoverflow.com/a/65924963/7740992
        keys = []
        f.visit(lambda key: keys.append(key) if isinstance(f[key], h5py.Dataset) else None)
        return keys

    @staticmethod
    def pick(h5_path, dat_path):
        tensors = HybridDict()
        tensors.chkfile.close()
        file_name = tensors.chkfile_name
        os.remove(file_name)
        shutil.copyfile(h5_path, file_name)
        tensors.chkfile = h5py.File(file_name, "r+")

        for key in HybridDict.get_dataset_keys(tensors.chkfile):
            tensors[key] = tensors.chkfile[key]

        with open(dat_path, "rb") as f:
            dct = pickle.load(f)
        tensors.update(dct)
        return tensors


def timing(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        t0, p0 = time(), process_time()
        result = f(*args, **kwargs)
        t1, p1 = time(), process_time()
        with open("tmp_timing.log", "a") as log:
            log.write(" {0:50s}, Wall: {1:10.3f} s, CPU: {2:10.3f} s, ratio {3:7.1f}%\n"
                  .format(f.__qualname__, t1-t0, p1-p0, (p1-p0)/(t1-t0) * 100))
        return result
    return wrapper


def parse_xc_dh(xc_dh: str):
    xc_dh = xc_dh.replace("-", "").replace("_", "").lower()
    return XC_DH_MAP[xc_dh]


def gen_batch(minval, maxval, nbatch):
    return [slice(i, (i + nbatch) if i + nbatch < maxval else maxval) for i in range(minval, maxval, nbatch)]


def gen_shl_batch(mol, blksize, start_id=0, stop_id=None):
    ao_loc = mol.ao_loc
    lst = balance_partition(ao_loc, blksize, start_id, stop_id)
    return [(t[0], t[1], ao_loc[t[0]], ao_loc[t[1]]) for t in lst]


def calc_batch_size(unit_flop, mem_avail, pre_flop=0):
    # mem_avail: in MB
    print("DEBUG: mem_avail", mem_avail)
    max_memory = 0.8 * mem_avail - pre_flop * 8 / 1024 ** 2
    batch_size = int(max(max_memory // (unit_flop * 8 / 1024 ** 2), 1))
    return batch_size


def get_rho_from_dm_gga(ni, mol, grids, dm):
    dm_shape = dm.shape
    dm = dm.reshape((-1, dm_shape[-2], dm_shape[-1]))
    nset, nao, _ = dm.shape
    rho = np.empty((nset, 4, grids.weights.size))
    ip = 0
    for ao, mask, weight, _ in ni.block_loop(mol, grids, nao, deriv=1):
        ngrid = weight.size
        for i in range(nset):
            rho[i, :, ip:ip+ngrid] = ni.eval_rho(mol, ao, dm[i], mask, "GGA", hermi=1)
        ip += ngrid
    rho.shape = list(dm_shape[:-2]) + list(rho.shape[-2:])
    return rho


def tot_size(*args):
    size = 0
    for i in args:
        if isinstance(i, np.ndarray):
            size += i.size
        else:
            size += tot_size(*i)
    return size


def restricted_biorthogonalize(t_ijab, cc, c_os, c_ss):
    # accomplish task: cc * ((c_os + c_ss) * t_ijab - c_ss * t_ijab.swapaxes(-1, -2))
    coef_0 = cc * (c_os + c_ss)
    coef_1 = - cc * c_ss
    # handle different situations
    if abs(coef_1) < 1e-7:  # SS, do not make transpose
        return coef_0 * t_ijab
    else:
        t_shape = t_ijab.shape
        t_ijab = t_ijab.reshape(-1, t_ijab.shape[-2], t_ijab.shape[-1])
        res = lib.transpose(t_ijab, axes=(0, 2, 1)).reshape(t_shape)
        t_ijab = t_ijab.reshape(t_shape)
        res *= coef_1
        res += coef_0 * t_ijab
        return res


def hermi_sum_last2dim_inplace(tsr, hermi=1):
    # shameless call lib.hermi_sum, just for a tensor wrapper
    tsr_shape = tsr.shape
    tsr.shape = (-1, tsr.shape[-1], tsr.shape[-2])
    res = lib.hermi_sum(tsr, axes=(0, 2, 1), hermi=hermi, inplace=True)
    res.shape = tsr_shape
    return res
