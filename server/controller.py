"""The paper's hysteresis-band / AIMD governance controller.

RE-PARAMETERIZED FOR THIS HARDWARE (say so on stage): the published constants
(eta=0.02, 5-min epochs, dead band 0.92*T_soft) were tuned against the sim's
550/1500 W A100-class node model. A rented 4090 has a much faster thermal loop,
so the demo runs shorter epochs against a *software governance setpoint* --
the operator's thermal budget, narrated as such, not the card's silicon limit.

The control LAW is the paper's, unchanged in shape:
  cool                 -> lam += eta         (creep toward energy)
  within the dead band -> hold               (damp chatter)
  over the setpoint    -> lam -= 6 * eta     (retreat hard; AIMD asymmetry)
  always               -> clip to [LAM_MIN, LAM_MAX]
"""
import os
import threading

ETA = float(os.environ.get("ETA", "0.02"))
BACKOFF = 6.0                                # the paper's -6*eta retreat
SETPOINT_C = float(os.environ.get("SETPOINT_C", "73"))   # governance setpoint
BAND = float(os.environ.get("BAND", "0.92"))             # dead band fraction
LAM_MIN, LAM_MAX = 0.15, 0.90                            # paper's clip bound
EPOCH_S = float(os.environ.get("EPOCH_S", "3"))          # demo-scale epoch


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "fixed"          # start in the strawman: fixed lambda
        self.lam = 0.85              # the "tuned elsewhere" aggressive pack
        self.last_action = "hold"

    def set_mode(self, mode, lam=None):
        with self.lock:
            self.mode = mode
            if lam is not None:
                self.lam = max(LAM_MIN, min(LAM_MAX, float(lam)))

    def step(self, tmax):
        """One epoch. tmax = max temperature over healthy GPUs (MEASURED)."""
        with self.lock:
            if self.mode != "auto":
                self.last_action = "fixed"
                return self.lam
            if tmax > SETPOINT_C:
                self.lam -= BACKOFF * ETA
                self.last_action = "retreat"
            elif tmax > BAND * SETPOINT_C:
                self.last_action = "hold"
            else:
                self.lam += ETA
                self.last_action = "advance"
            self.lam = max(LAM_MIN, min(LAM_MAX, self.lam))
            return self.lam

    def snapshot(self):
        with self.lock:
            return {"mode": self.mode, "lam": round(self.lam, 3),
                    "action": self.last_action, "setpoint_c": SETPOINT_C,
                    "band_c": round(BAND * SETPOINT_C, 1),
                    "eta": ETA, "clip": [LAM_MIN, LAM_MAX]}
