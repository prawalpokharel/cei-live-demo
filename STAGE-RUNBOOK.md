# Demo-day pre-flight — print this

Full plan: the Live GPU Demo Runbook artifact. This page is the checklist.

## The week before
- [ ] Email organizers: slot length · house-laptop policy (get HDMI approval
      in writing if needed) · what "Presentation Submission" (Sep 24) wants
- [ ] Dress rehearsal on the SAME config (6×4090 RunPod Secure) — tune the
      setpoint from the measured plateau (plateau − 4 °C)
- [ ] Record the clean run: OBS, 1080p, **MKV**, identical layout →
      local disk **and** USB stick  (this recording is the baseline; live is
      the upgrade)
- [ ] Flash + bench-test the dial on the phone hotspot; label the box
- [ ] Test the USB button on the dashboard (A to arm → press fires kill)

## T-minus 90 min
- [ ] Provision the pod (Secure Cloud, whole cards) — `./demo.sh`
- [ ] `nvidia-smi` power check: **no N/A** anywhere
- [ ] Open the proxy URL **from your phone's cellular** at the room location
- [ ] Warm-up burn 2 min: confirm temps climb past the setpoint, then reset
- [ ] Capture the fixed-λ ghost (kill → Save as ghost → Reset)
- [ ] Start FIXED λ=0.85 idle-hold; leave tmux attached on the tether

## T-minus 10 min
- [ ] Laptop on **USB phone tether** (venue wifi never in the loop)
- [ ] Dashboard tab open + fullscreen; recording file open in a second tab
- [ ] Dial powered, polling (OLED shows λ); knob at 0.85
- [ ] Button plugged in, NOT armed; volunteer identified (or session chair)

## On stage
- Cold open → talk → demo at ~min 8: Start stream → pain (fixed) → AUTO →
  ARM (A) → hand the button → resolve the bet → receipt slide.
- **45-second rule:** anything misbehaves for 45 s → the line — *"real
  hardware doesn't always cooperate on cue; here's the run I captured this
  morning"* — and play the MKV. Never debug on stage.

## After
- [ ] Stop the pod (per-second billing stops)
- [ ] Screenshot the billing page → the "receipt" for the closing slide/post
