# Intent: a fast, incremental drop-in for Freerouting in pcbkit (12 Oct 2026)

## Problem (Bruk's words, condensed)
pcbkit routes boards by calling Freerouting 1.9.0 on the command line with Specctra files
(`java -jar freerouting.jar -de board.dsn -do board.ses -mp 40`). Boards are edited many times and
almost every edit is small: a capacitor moved 2-3 mm, a value changed, a net class tweaked, a
hand-laid stub added. Each regeneration costs up to 3 tries of about 4 minutes on the spotmicro
carrier (127 x 95 mm, two layers, about 160 parts). pcbkit's eco route (copy the old route, lock
most of it) is unreliable: after the last edit it could not connect two PWM nets in any of three
tries, though a path existed, because locked copper boxed them in.

## Outcome wanted
A router pcbkit can call in place of Freerouting that is fast, and above all reroutes incrementally:
given the previous session, it keeps what still fits and rips up and reroutes what an edit disturbs,
nothing locked except hand-laid copper.

## What pcbkit's wrapper relies on (must provide)
1. The `-de` / `-do` / `-mp` command line, writing a SES that pcbnew imports with ImportSpecctraSES.
2. A log line marking the start of real routing ("Starting auto-routing"), for the stall watchdog.
3. A time limit: the router stops itself and writes its best result; ideally also on SIGTERM.
4. A minimum track width option, so routes are never narrowed below the board minimum.
5. A way to search differently on a retry (a seed) if results are deterministic.
Agreed additions: `--board <file.kicad_pcb>` (read geometry and rules from KiCad, closing the
per-pad clearance gap) and `--previous <old.ses>` (incremental reroute).

## Acceptance
- A. On the spotmicro carrier as it stands (kicad/prerouted.kicad_pcb + DSN), a full route whose SES
     imports into pcbnew 10 and leaves no copper problem in KiCad DRC (pcbkit's COPPER_CATEGORIES:
     unconnected, clearance, shorts, crossing, hole, edge), or fewer than Freerouting's best of 3.
- B. The four-capacitor edit (golden/ -> kicad/, C2, C41, C50, C51 moved), routed with `--previous
     golden/spotmicro_carrier.ses`: copper-clean as in A, in a small fraction of a full route's time,
     with most of the previous route unchanged.
- C. Wall time reported against Freerouting 1.9.0 on the same files and machine.
- D. Items 1-5 above demonstrated by a test each.

## Must not break
The research harness and its frozen experiments in ../ (negotiate3/4/5, gate*.py, tools/kicad_io*.py
stay as they are; new code lives in dropin/).

## Open questions (carried forward)
- Pours: pcbkit fills pours after routing, so every pad still gets a track. Leaving pour-net pads to
  the pour is a later option, not in scope now.
- Keep-outs that make Freerouting spin: we should fail fast instead; checked when seen.
- Freerouting 1.9.0 on this machine: needs Java 21 and the 1.9.0 jar for C.
