"""
Copyright (C) 2015-2025 Laurent Courty

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""
cimport cython
from cython.parallel cimport prange
from libc.math cimport pow as c_pow
from libc.math cimport sqrt as c_sqrt
from libc.math cimport atan2 as c_atan
from libc.math cimport hypot, fmax, fmin

ctypedef cython.floating DTYPE_t
cdef float PI = 3.1415926535898


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
def apply_hydrology(
    DTYPE_t[:, :] arr_rain,
    DTYPE_t[:, :] arr_inf,
    DTYPE_t[:, :] arr_capped_losses,
    DTYPE_t[:, :] arr_h,
    DTYPE_t[:, :] arr_eff_precip,
    DTYPE_t dt,
):
    """Update arr_eff_precip in m/s
    rain and infiltration in m/s, deph in m, dt in seconds"""
    cdef int rmax, cmax, r, c
    cdef DTYPE_t hydro_raw, hydro_capped, losses_limit
    rmax = arr_rain.shape[0]
    cmax = arr_rain.shape[1]
    for r in prange(rmax, nogil=True):
        for c in range(cmax):
            hydro_raw = arr_rain[r, c] - arr_inf[r, c] - arr_capped_losses[r, c]
            losses_limit = - arr_h[r, c] / dt
            hydro_capped = max(losses_limit, hydro_raw)
            arr_eff_precip[r, c] = hydro_capped


@cython.wraparound(False)  # Disable negative index check
@cython.boundscheck(False)  # turn off bounds-checking for entire function
def flow_dir(
    DTYPE_t[:, :] arr_max_dz,
    DTYPE_t[:, :] arr_dz0,
    DTYPE_t[:, :] arr_dz1,
    DTYPE_t[:, :] arr_dir
):
    """Update arr_dir with a rain-routing direction:
    0: the flow is going dowstream, index-wise
    1: the flow is going upstream, index-wise
    -1: no routing happening on that face
    """
    cdef int rmax, cmax, r, c
    cdef DTYPE_t max_dz, dz0, dz1, qdir
    rmax = arr_max_dz.shape[0]
    cmax = arr_max_dz.shape[1]
    for r in prange(rmax, nogil=True):
        for c in range(cmax):
            max_dz = arr_max_dz[r, c]
            dz0 = arr_dz0[r, c]
            dz1 = arr_dz1[r, c]
            qdir = arr_dir[r, c]
            if max_dz > 0:
                if max_dz == dz0:
                    qdir = 0
                elif max_dz == dz1:
                    qdir = 1
                else:
                    qdir = -1
            else:
                qdir = -1
            # update results array
            arr_dir[r, c] = qdir


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
@cython.initializedcheck(False)  # Skip initialization checks for performance
@cython.nonecheck(False)  # Skip None checks for performance
def solve_q(
    DTYPE_t[:, ::1] arr_dire,
    DTYPE_t[:, ::1] arr_dirs,
    DTYPE_t[:, ::1] arr_z,
    DTYPE_t[:, ::1] arr_n,
    DTYPE_t[:, ::1] arr_h,
    DTYPE_t[:, ::1] arr_qe,
    DTYPE_t[:, ::1] arr_qs,
    DTYPE_t[:, ::1] arr_hfe,
    DTYPE_t[:, ::1] arr_hfs,
    DTYPE_t[:, ::1] arr_qe_new,
    DTYPE_t[:, ::1] arr_qs_new,
    DTYPE_t dt,
    DTYPE_t dx,
    DTYPE_t dy,
    DTYPE_t g,
    DTYPE_t theta,
    DTYPE_t hf_min,
    DTYPE_t v_rout,
    DTYPE_t sl_thres,
):
    """Calculate hflow in m, flow in m2/s.
    Expect padded arrays.
    """

    cdef int rmax, cmax, r, c, rp, cp
    cdef DTYPE_t wse_e, wse_s, wse0, z0, ze, zs, n0, n, ne, ns
    cdef DTYPE_t qe_st, qs_st, qe, qs, qe_vect, qs_vect, qdire, qdirs
    cdef DTYPE_t qe_new, qs_new, hf_e, hf_s, h0, h_e, h_s

    rmax = arr_z.shape[0] - 1
    cmax = arr_z.shape[1] - 1
    for r in prange(1, rmax, nogil=True):
        for c in range(1, cmax):
            # values at the current cell
            z0 = arr_z[r, c]
            h0 = arr_h[r, c]
            wse0 = z0 + h0
            n0 = arr_n[r,c]
            qe = arr_qe[r,c]
            qs = arr_qs[r,c]

            # x dimension, flow at E cell boundary
            # prevent calculation of domain boundary
            # range(10) is from 0 to 9, so last cell is max - 1
            if c < (cmax - 1):
                # flow routing direction
                qdire = arr_dire[r, c]
                # water surface elevation
                ze = arr_z[r, c+1]
                h_e = arr_h[r, c+1]
                wse_e = ze + h_e
                # average friction
                ne = 0.5 * (n0 + arr_n[r,c+1])
                # calculate average flow from stencil
                qe_st = .25 * (qs + arr_qs[r-1,c] +
                               arr_qs[r-1,c+1] + arr_qs[r,c+1])
                # calculate qnorm using vectorizable hypot
                qe_vect = hypot(qe, qe_st)
                # hflow
                hf_e = hflow(z0=z0, z1=ze, wse0=wse0, wse1=wse_e)
                arr_hfe[r, c] = hf_e
                # flow and velocity
                if hf_e <= 0:
                    qe_new = 0
                elif hf_e > hf_min:
                    qe_new = almeida2013(hf=hf_e, wse0=wse0, wse1=wse_e, n=ne,
                                         qm1=arr_qe[r,c-1], q0=qe,
                                         qp1=arr_qe[r,c+1], q_norm=qe_vect,
                                         theta=theta, g=g, dt=dt, cell_len=dx)
                # flow routing going W, i.e negative
                elif hf_e <= hf_min and qdire == 0 and wse_e > wse0:
                    qe_new = - rain_routing(h_e, wse_e, wse0,
                                            dt, dx, v_rout)
                # flow routing going E, i.e positive
                elif hf_e <= hf_min and  qdire == 1 and wse0 > wse_e:
                    qe_new = rain_routing(h0, wse0, wse_e,
                                          dt, dx, v_rout)
                else:
                    qe_new = 0
                # udpate array
                arr_qe_new[r, c] = qe_new

            # y dimension, flow at S cell boundary
            if r < (rmax - 1):  # prevent calculation of domain boundary
                # flow routing direction
                qdirs = arr_dirs[r, c]
                # water surface elevation
                zs = arr_z[r+1, c]
                h_s = arr_h[r+1, c]
                wse_s = zs + h_s
                # average friction
                ns = 0.5 * (n0 + arr_n[r+1,c])
                # calculate average flow from stencil
                qs_st = .25 * (qe + arr_qe[r+1,c] +
                               arr_qe[r+1,c-1] + arr_qe[r,c-1])
                # calculate qnorm using vectorizable hypot
                qs_vect = hypot(qs, qs_st)
                # hflow
                hf_s = hflow(z0=z0, z1=zs, wse0=wse0, wse1=wse_s)
                arr_hfs[r, c] = hf_s
                if hf_s <= 0:
                    qs_new = 0
                elif hf_s > hf_min:
                    qs_new = almeida2013(hf=hf_s, wse0=wse0, wse1=wse_s, n=ns,
                                         qm1=arr_qs[r-1,c], q0=qs,
                                         qp1=arr_qs[r+1,c], q_norm=qs_vect,
                                         theta=theta, g=g, dt=dt, cell_len=dy)
                # flow routing going N, i.e negative
                elif hf_s <= hf_min and qdirs == 0 and wse_s > wse0:
                    qs_new = - rain_routing(h_s, wse_s, wse0,
                                            dt, dy, v_rout)
                # flow routing going S, i.e positive
                elif hf_s <= hf_min and  qdirs == 1 and wse0 > wse_s:
                    qs_new = rain_routing(h0, wse0, wse_s,
                                          dt, dy, v_rout)
                else:
                    qs_new = 0
                # udpate array
                arr_qs_new[r, c] = qs_new


cdef DTYPE_t hflow(DTYPE_t z0, DTYPE_t z1, DTYPE_t wse0, DTYPE_t wse1) noexcept nogil:
    """calculate flow depth
    """
    return max(wse1, wse0) - max(z1, z0)


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
cdef DTYPE_t almeida2013(
    DTYPE_t hf,
    DTYPE_t wse0,
    DTYPE_t wse1,
    DTYPE_t n,
    DTYPE_t qm1,
    DTYPE_t q0,
    DTYPE_t qp1,
    DTYPE_t q_norm,
    DTYPE_t theta,
    DTYPE_t g,
    DTYPE_t dt,
    DTYPE_t cell_len,
) noexcept nogil:
    """Solve flow using q-centered scheme from Almeida et Al. (2013)
    """
    cdef DTYPE_t term_1, term_2, term_3, slope

    slope = (wse0 - wse1) / cell_len

    term_1 = theta * q0 + (1 - theta) * (qm1 + qp1) * 0.5
    term_2 = g * hf * dt * slope
    term_3 = 1 + g * dt * (n*n) * q_norm / c_pow(hf, 7./3.)
    # If flow direction is not coherent with surface slope,
    # use only previous flow, i.e. ~switch to Bates 2010
    if term_1 * term_2 < 0:
        term_1 = q0
    return (term_1 + term_2) / term_3


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
cdef DTYPE_t rain_routing(
    DTYPE_t h0,
    DTYPE_t wse0,
    DTYPE_t wse1,
    DTYPE_t dt,
    DTYPE_t cell_len,
    DTYPE_t v_routing,
) noexcept nogil:
    """Calculate flow routing at a face in m2/s
    Cf. Sampson et al. (2013)
    """
    cdef DTYPE_t maxflow, q_routing, dh
    # fraction of the depth to be routed
    dh = wse0 - wse1
    # make sure it's positive (should not happend, checked before)
    dh = max(dh, 0)
    # if WSE of destination cell is below the dem of the drained cell, set to h0
    dh = min(dh, h0)
    maxflow = cell_len * dh / dt
    q_routing = min(dh * v_routing, maxflow)
    return q_routing


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
@cython.initializedcheck(False)  # Skip initialization checks for performance
@cython.nonecheck(False)  # Skip None checks for performance
def solve_h(
    DTYPE_t[:, ::1] arr_ext,
    DTYPE_t[:, ::1] arr_qe,
    DTYPE_t[:, ::1] arr_qs,
    DTYPE_t[:, ::1] arr_bct,
    DTYPE_t[:, ::1] arr_bcv,
    DTYPE_t[:, ::1] arr_h,
    DTYPE_t[:, ::1] arr_hmax,
    DTYPE_t[:, ::1] arr_hfix,
    DTYPE_t[:, ::1] arr_herr,
    DTYPE_t[:, ::1] arr_hfe,
    DTYPE_t[:, ::1] arr_hfs,
    DTYPE_t[:, ::1] arr_v,
    DTYPE_t[:, ::1] arr_vdir,
    DTYPE_t[:, ::1] arr_vmax,
    DTYPE_t[:, ::1] arr_fr,
    DTYPE_t dx,
    DTYPE_t dy,
    DTYPE_t dt,
    DTYPE_t g
):
    """Update the water depth and max depth
    Adjust water depth according to in-domain 'boundary' condition
    Calculate vel. magnitude in m/s, direction in degree and Froude number.
    """
    cdef int rmax, cmax, r, c
    cdef DTYPE_t qext, qe, qw, qn, qs, h, q_sum, h_new, hmax, bct, bcv
    cdef DTYPE_t hfe, hfs, hfw, hfn, ve, vw, vn, vs, vx, vy, v, vdir
    cdef DTYPE_t eps = 1e-12  # Small epsilon to avoid division by zero

    rmax = arr_ext.shape[0] - 1
    cmax = arr_ext.shape[1] - 1
    for r in prange(1, rmax, nogil=True):
        for c in range(1, cmax):
            qext = arr_ext[r, c]
            qe = arr_qe[r, c]
            qw = arr_qe[r, c-1]
            qn = arr_qs[r-1, c]
            qs = arr_qs[r, c]
            bct = arr_bct[r, c]
            bcv = arr_bcv[r, c]
            h = arr_h[r, c]
            hmax = arr_hmax[r, c]
            # Sum of flows in m/s
            q_sum = (qw - qe) / dx + (qn - qs) / dy
            # calculate new flow depth
            h_new = h + (qext + q_sum) * dt
            if h_new < 0.:
                # Write error. Always positive (mass creation)
                arr_herr[r, c] += - h_new
                h_new = 0.
            # Apply fixed water level
            if bct == 4:
                # Positive if water enters the domain
                arr_hfix[r, c] += bcv - h_new
                h_new = bcv
            # Update max depth array
            arr_hmax[r, c] = max(h_new, hmax)
            # Update depth array
            arr_h[r, c] = h_new

            ## Velocity and Froude ##
            hfe = arr_hfe[r, c]
            hfw = arr_hfe[r, c-1]
            hfn = arr_hfs[r-1, c]
            hfs = arr_hfs[r, c]
            # Branchless velocity calculations for vectorization
            # Use fmax to avoid division by zero,
            # then multiply by zero or one by using boolean operation
            ve = qe / fmax(hfe, eps) * (hfe > 0.)
            vw = qw / fmax(hfw, eps) * (hfw > 0.)
            vs = qs / fmax(hfs, eps) * (hfs > 0.)
            vn = qn / fmax(hfn, eps) * (hfn > 0.)
            # Velocities at the center of the cell
            vx = .5 * (ve + vw)
            vy = .5 * (vs + vn)

            # velocity magnitude and direction
            v = hypot(vx, vy)
            arr_v[r, c] = v
            arr_vmax[r, c] = max(v, arr_vmax[r, c])
            vdir = c_atan(-vy, vx) * 180. / PI
            # Branchless. Add 360 only to negative numbers
            vdir = vdir + 360. * (vdir < 0)
            arr_vdir[r, c] = vdir

            # Froude number
            arr_fr[r, c] = v / c_sqrt(g * h_new)


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
def infiltration_user(
    DTYPE_t[:, :] arr_h,
    DTYPE_t[:, :] arr_inf_in,
    DTYPE_t[:, :] arr_inf_out,
    DTYPE_t dt
):
    """Calculate infiltration rate using a user-defined fixed rate
    """
    cdef int rmax, cmax, r, c

    rmax = arr_h.shape[0]
    cmax = arr_h.shape[1]
    for r in prange(rmax, nogil=True):
        for c in range(cmax):
            # cap the rate
            arr_inf_out[r, c] = cap_infiltration_rate(dt, arr_h[r, c], arr_inf_in[r, c])


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
def infiltration_ga(
    DTYPE_t[:, :] arr_h,
    DTYPE_t[:, :] arr_eff_por,
    DTYPE_t[:, :] arr_pressure,
    DTYPE_t[:, :] arr_conduct,
    DTYPE_t[:, :] arr_inf_amount,
    DTYPE_t[:, :] arr_water_soil_content,
    DTYPE_t[:, :] arr_inf_out,
    DTYPE_t dt
):
    """Calculate infiltration rate using the Green-Ampt formula
    """
    cdef int rmax, cmax, r, c
    cdef DTYPE_t infrate, avail_porosity, poros_cappress, conduct
    rmax = arr_h.shape[0]
    cmax = arr_h.shape[1]
    for r in prange(rmax, nogil=True):
        for c in range(cmax):
            conduct = arr_conduct[r, c]
            avail_porosity = max(arr_eff_por[r, c] - arr_water_soil_content[r, c], 0)
            poros_cappress = avail_porosity * (arr_pressure[r, c] + arr_h[r, c])
            infrate = conduct * (1 + (poros_cappress / arr_inf_amount[r, c]))
            # cap the rate
            infrate = cap_infiltration_rate(dt, arr_h[r, c], infrate)
            # update total infiltration amount
            arr_inf_amount[r, c] += infrate * dt
            # populate output infiltration array
            arr_inf_out[r, c] = infrate


@cython.wraparound(False)  # Disable negative index check
@cython.cdivision(True)  # Don't check division by zero
@cython.boundscheck(False)  # turn off bounds-checking for entire function
cdef DTYPE_t cap_infiltration_rate(DTYPE_t dt, DTYPE_t h, DTYPE_t infrate) noexcept nogil:
    """Cap the infiltration rate to not generate negative depths
    """
    return min(h / dt, infrate)
