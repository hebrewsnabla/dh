from __future__ import annotations

from pyscf.dft.numint import _scale_ao, _dot_ao_ao
from pyscf.scf import cphf

import dh.rdfdh
from dh.dhutil import calc_batch_size, gen_batch
from pyscf import gto, lib, dft
import numpy as np

einsum = lib.einsum


def kernel(mf_dh: Polar):
    mf_dh.run_scf()
    mf_dh.prepare_H_1()
    mf_dh.prepare_integral()
    mf_dh.prepare_xc_kernel()
    mf_dh.prepare_pt2(dump_t_ijab=True)
    mf_dh.prepare_lagrangian(gen_W=False)
    mf_dh.prepare_D_r()
    mf_dh.prepare_U_1()
    if dft.numint.NumInt()._xc_type(mf_dh.xc) == "GGA":
        mf_dh.prepare_dmU()
        mf_dh.prepare_polar_Ax1_gga()
    mf_dh.prepare_pdA_F_0_mo()
    mf_dh.prepare_pdA_Y_ia_ri()
    mf_dh.prepare_pt2_deriv()
    mf_dh.prepare_polar()
    return mf_dh.de


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


def _rks_gga_wv2(rho0, rho1, rho2, fxc, kxc, weight):
    frr, frg, fgg = fxc[:3]
    frrr, frrg, frgg, fggg = kxc

    sigma01 = 2 * einsum("rg, rg -> g", rho0[1:], rho1[1:])
    sigma02 = 2 * einsum("rg, rg -> g", rho0[1:], rho2[1:])
    sigma12 = 2 * einsum("rg, rg -> g", rho1[1:], rho2[1:])
    r1r2 = rho1[0] * rho2[0]
    r1s2 = rho1[0] * sigma02
    s1r2 = sigma01 * rho2[0]
    s1s2 = sigma01 * sigma02

    wv = np.zeros((4, frr.size))
    wv1_tmp = np.zeros(frr.size)

    wv[0] += frrr * r1r2
    wv[0] += frrg * r1s2
    wv[0] += frrg * s1r2
    wv[0] += frgg * s1s2
    wv[0] += frg * sigma12

    wv1_tmp += frrg * r1r2
    wv1_tmp += frgg * r1s2
    wv1_tmp += frgg * s1r2
    wv1_tmp += fggg * s1s2
    wv1_tmp += fgg * sigma12
    wv[1:] += wv1_tmp * rho0[1:]

    wv[1:] += frg * rho1[0] * rho2[1:]
    wv[1:] += frg * rho2[0] * rho1[1:]
    wv[1:] += fgg * sigma01 * rho2[1:]
    wv[1:] += fgg * sigma02 * rho1[1:]

    wv[0] *= 0.5
    wv[1:] *= 2

    wv *= weight
    return wv


class Polar(dh.rdfdh.RDFDH):

    def __init__(self, mol: gto.Mole, skip_construct=False, *args, **kwargs):
        if not skip_construct:
            super(Polar, self).__init__(mol, *args, **kwargs)
        self.pol_scf = NotImplemented
        self.pol_corr = NotImplemented
        self.pol_tot = NotImplemented
        self.de = NotImplemented

    def prepare_H_1(self):
        tensors = self.tensors
        mol, C = self.mol, self.C
        H_1_ao = - mol.intor("int1e_r")
        H_1_mo = C.T @ H_1_ao @ C
        tensors.create("H_1_ao", H_1_ao)
        tensors.create("H_1_mo", H_1_mo)

    def prepare_U_1(self):
        tensors = self.tensors
        sv, so = self.sv, self.so

        H_1_mo = tensors.load("H_1_mo")
        U_1_vo = cphf.solve(self.Ax0_cpks(), self.e, self.mo_occ, H_1_mo[:, sv, so], max_cycle=self.cpks_cyc, tol=self.cpks_tol)[0]
        U_1 = np.zeros_like(H_1_mo)
        U_1[:, sv, so] = U_1_vo
        U_1[:, so, sv] = - U_1_vo.swapaxes(-1, -2)
        tensors.create("U_1", U_1)

    def prepare_dmU(self):
        tensors = self.tensors
        U_1 = tensors.load("U_1")
        D_r = tensors.load("D_r")
        rho = tensors.load("rho")
        C, Co = self.C, self.Co
        so = self.so
        mol, ni, grids, xc = self.mol, self.mf._numint, self.mf.grids, self.xc
        ni.libxc = dft.xcfun
        dmU = C @ U_1[:, :, so] @ Co.T
        dmU += dmU.swapaxes(-1, -2)
        dmDr = C @ D_r @ C.T
        dmDr += dmDr.swapaxes(-1, -2)
        dmX = np.concatenate([dmU, [dmDr]])
        rhoX = get_rho_from_dm_gga(ni, mol, grids, dmX)
        _, _, _, kxc = ni.eval_xc(xc, rho, spin=0, deriv=3)
        tensors.create("rhoU", rhoX[:-1])
        tensors.create("rhoDr", rhoX[-1])
        tensors.create("kxc" + xc, kxc)

    def prepare_pdA_F_0_mo(self):
        tensors = self.tensors
        so, sa = self.so, self.sa

        pdA_F_0_mo = np.array(tensors.load("H_1_mo"))
        U_1 = tensors.load("U_1")

        pdA_F_0_mo += einsum("Apq, p -> Apq", U_1, self.e)
        pdA_F_0_mo += einsum("Aqp, q -> Apq", U_1, self.e)
        pdA_F_0_mo += self.Ax0_Core(sa, sa, sa, so)(U_1[:, :, so])
        tensors.create("pdA_F_0_mo", pdA_F_0_mo)

        if self.mf_n:
            F_0_mo_n = einsum("up, uv, vq -> pq", self.C, self.mf_n.get_fock(dm=self.D), self.C)
            pdA_F_0_mo_n = np.array(tensors.load("H_1_mo"))
            pdA_F_0_mo_n += einsum("Amp, mq -> Apq", U_1, F_0_mo_n)
            pdA_F_0_mo_n += einsum("Amq, pm -> Apq", U_1, F_0_mo_n)
            pdA_F_0_mo_n += self.Ax0_Core(sa, sa, sa, so, xc=self.xc_n)(U_1[:, :, so])
            tensors.create("pdA_F_0_mo_n", pdA_F_0_mo_n)

    def prepare_pdA_Y_ia_ri(self):
        tensors = self.tensors
        U_1 = tensors.load("U_1")
        Y_mo_ri = tensors["Y_mo_ri"]
        nocc, nvir, nmo, naux = self.nocc, self.nvir, self.nmo, self.df_ri.get_naoaux()
        so, sv = self.so, self.sv

        pdA_Y_ia_ri = np.zeros((3, naux, nocc, nvir))
        nbatch = calc_batch_size(8 * nmo**2, self.get_memory(), U_1.size)
        for saux in gen_batch(0, self.aux_ri.nao, nbatch):
            pdA_Y_ia_ri[:, saux] = (
                + einsum("Ami, Pma -> APia", U_1[:, :, so], Y_mo_ri[saux, :, sv])
                + einsum("Ama, Pmi -> APia", U_1[:, :, sv], Y_mo_ri[saux, :, so]))
        tensors.create("pdA_Y_ia_ri", pdA_Y_ia_ri)

    def prepare_pt2_deriv(self):
        tensors = self.tensors
        nocc, nvir, nmo, naux = self.nocc, self.nvir, self.nmo, self.df_ri.get_naoaux()
        so, sv = self.so, self.sv
        eo, ev = self.eo, self.ev

        pdA_F_0_mo = tensors.load("pdA_F_0_mo")
        Y_ia_ri = np.asarray(tensors["Y_mo_ri"][:, so, sv])
        pdA_Y_ia_ri = tensors["pdA_Y_ia_ri"]

        pdA_G_ia = tensors.create("pdA_G_ia", shape=(3, naux, nocc, nvir))
        pdA_D_rdm1 = tensors.create("pdA_D_rdm1", shape=(3, nmo, nmo))

        nbatch = calc_batch_size(2 * 8*nocc*nvir**2, self.get_memory(), Y_ia_ri.size + pdA_F_0_mo.size + pdA_Y_ia_ri.size)
        D_jab = eo[None, :, None, None] - ev[None, None, :, None] - ev[None, None, None, :]
        for sI in gen_batch(0, nocc, 2 * nbatch):
            t_ijab = np.asarray(tensors["t_ijab"][sI])
            D_ijab = eo[sI, None, None, None] + D_jab

            pdA_t_ijab = einsum("APia, Pjb -> Aijab", pdA_Y_ia_ri[:, :, sI], Y_ia_ri)
            pdA_t_ijab += einsum("APjb, Pia -> Aijab", pdA_Y_ia_ri, Y_ia_ri[:, sI])

            for sK in gen_batch(0, nocc, nbatch):
                t_kjab = t_ijab if sK == sI else tensors["t_ijab"][sK]
                pdA_t_ijab -= einsum("Aki, kjab -> Aijab", pdA_F_0_mo[:, sK, sI], t_kjab)
            pdA_t_ijab -= einsum("Akj, ikab -> Aijab", pdA_F_0_mo[:, so, so], t_ijab)
            pdA_t_ijab += einsum("Acb, ijac -> Aijab", pdA_F_0_mo[:, sv, sv], t_ijab)
            pdA_t_ijab += einsum("Aca, ijcb -> Aijab", pdA_F_0_mo[:, sv, sv], t_ijab)
            pdA_t_ijab /= D_ijab

            cc, c_os, c_ss = self.cc, self.c_os, self.c_ss
            T_ijab = cc * ((c_os + c_ss) * t_ijab - c_ss * t_ijab.swapaxes(-1, -2))
            pdA_T_ijab = cc * ((c_os + c_ss) * pdA_t_ijab - c_ss * pdA_t_ijab.swapaxes(-1, -2))

            pdA_G_ia[:, :, sI] += einsum("Aijab, Pjb -> APia", pdA_T_ijab, Y_ia_ri)
            pdA_G_ia[:, :, sI] += einsum("ijab, APjb -> APia", T_ijab, pdA_Y_ia_ri)

            pdA_D_rdm1[:, so, so] -= 2 * einsum("kiab, Akjab -> Aij", T_ijab, pdA_t_ijab)
            pdA_D_rdm1[:, sv, sv] += 2 * einsum("ijac, Aijbc -> Aab", T_ijab, pdA_t_ijab)
        pdA_D_rdm1[:] += pdA_D_rdm1.swapaxes(-1, -2)

    def prepare_polar_Ax1_gga(self):
        tensors = self.tensors
        U_1 = tensors.load("U_1")
        rho = tensors.load("rho")
        rhoU = tensors.load("rhoU")
        rhoDr = tensors.load("rhoDr")
        fxc = tensors["fxc" + self.xc]
        kxc = tensors["kxc" + self.xc]

        mol, ni, grids = self.mol, self.mf._numint, self.mf.grids
        nao = self.nao
        C, Co, so = self.C, self.Co, self.so

        Ax1 = np.zeros((3, nao, nao))
        wv2 = np.empty((3, 4, grids.weights.size))
        for i in range(3):
            wv2[i] = _rks_gga_wv2(rho, rhoU[i], rhoDr, fxc, kxc, grids.weights)
        ip = 0
        for ao, mask, weight, _ in ni.block_loop(mol, grids, nao, deriv=1):
            sg = slice(ip, ip + weight.size)
            for i in range(3):
                # v = einsum("rg, rgu, gv -> uv", wv2[i, :, sg], ao, ao[0])
                aow = _scale_ao(ao, wv2[i, :, sg])
                v = _dot_ao_ao(mol, aow, ao[0], mask, None, None)
                Ax1[i] += 2 * (v + v.T)
            ip += weight.size
        res = lib.einsum("Auv, um, vi, Bmi -> AB", Ax1, C, Co, U_1[:, :, so])
        tensors.create("Ax1_contrib", res)

    def get_SCR3(self):
        tensors = self.tensors
        so, sv, sa = self.so, self.sv, self.sa

        U_1 = tensors.load("U_1")
        G_ia = tensors.load("G_ia")
        pdA_G_ia = tensors["pdA_G_ia"]
        Y_mo_ri = tensors["Y_mo_ri"]

        SCR3 = np.zeros((3, self.nvir, self.nocc))
        nbatch = calc_batch_size(10 * self.nmo**2, self.get_memory(), G_ia.size + pdA_G_ia.size)
        for saux in gen_batch(0, self.aux_ri.nao, nbatch):
            Y_mo_ri_blk = np.asarray(Y_mo_ri[saux])
            pdA_G_ia_ri_blk = np.asarray(pdA_G_ia[:, saux])

            pdA_Y_mo_ri_blk = einsum("Ami, Pmj -> APij", U_1[:, :, so], Y_mo_ri_blk[:, :, so])
            pdA_Y_mo_ri_blk += pdA_Y_mo_ri_blk.swapaxes(-1, -2)
            SCR3 -= 4 * einsum("APja, Pij -> Aai", pdA_G_ia_ri_blk, Y_mo_ri_blk[:, so, so])
            SCR3 -= 4 * einsum("Pja, APij -> Aai", G_ia[saux], pdA_Y_mo_ri_blk)

            pdA_Y_mo_ri_blk = einsum("Ama, Pmb -> APab", U_1[:, :, sv], Y_mo_ri_blk[:, :, sv])
            pdA_Y_mo_ri_blk += pdA_Y_mo_ri_blk.swapaxes(-1, -2)
            SCR3 += 4 * einsum("APib, Pab -> Aai", pdA_G_ia_ri_blk, Y_mo_ri_blk[:, sv, sv])
            SCR3 += 4 * einsum("Pib, APab -> Aai", G_ia[saux, :, :], pdA_Y_mo_ri_blk)
        if self.mf_n:
            pdA_F_0_mo_n = tensors.load("pdA_F_0_mo_n")
            SCR3 += 4 * pdA_F_0_mo_n[:, sv, so]

        return SCR3

    def prepare_polar(self):
        tensors = self.tensors

        so, sv, sa = self.so, self.sv, self.sa

        H_1_mo = tensors.load("H_1_mo")
        U_1 = tensors.load("U_1")
        pdA_F_0_mo = tensors.load("pdA_F_0_mo")
        D_r = tensors.load("D_r")
        pdA_D_rdm1 = tensors.load("pdA_D_rdm1")

        SCR1 = self.Ax0_Core(sa, sa, sa, sa)(D_r)
        SCR2 = H_1_mo + self.Ax0_Core(sa, sa, sv, so)(U_1[:, sv, so])

        SCR3 = self.get_SCR3()

        pol_scf = - 4 * einsum("Api, Bpi -> AB", H_1_mo[:, :, so], U_1[:, :, so])
        pol_corr = - (
            + einsum("Aai, Bma, mi -> AB", U_1[:, sv, so], U_1[:, :, sv], SCR1[:, so])
            + einsum("Aai, Bmi, ma -> AB", U_1[:, sv, so], U_1[:, :, so], SCR1[:, sv])
            + einsum("Apm, Bmq, pq -> AB", SCR2, U_1, D_r)
            + einsum("Amq, Bmp, pq -> AB", SCR2, U_1, D_r)
            + einsum("Apq, Bpq -> AB", SCR2, pdA_D_rdm1)
            + einsum("Bai, Aai -> AB", SCR3, U_1[:, sv, so])
            - einsum("Bki, Aai, ak -> AB", pdA_F_0_mo[:, so, so], U_1[:, sv, so], D_r[sv, so])
            + einsum("Bca, Aai, ci -> AB", pdA_F_0_mo[:, sv, sv], U_1[:, sv, so], D_r[sv, so]))
        if self.xc != "HF":
            pol_corr -= 2 * tensors.load("Ax1_contrib")

        self.pol_scf = pol_scf
        self.pol_corr = pol_corr
        self.de = self.pol_tot = pol_scf + pol_corr

    kernel = kernel
