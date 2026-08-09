# Backlog — owner feedback, 2026-08-09

Raised after installing the build and opening it. **Nothing here is started.**
Recorded verbatim first, then what it means in code, so the next session works
from the owner's list rather than re-deriving one.

---

## 1. It opens empty

> *"It opens empty"*

The installer creates a fresh database in `%LOCALAPPDATA%\AIvionics\data`, so a
new install shows a login, a forced password change, and then blank screens.
The 1.75 M reports, 2,426 AMM tasks, 8,194 indexed locators and the fitted
calibration all sit in the project folder and the installed application never
sees them.

This is the biggest one. Technically correct, practically useless: the first
thing the owner saw was nothing.

Options, in the order they should be considered:
- **Ship the database with the installer.** Simple, honest, ~1 GB. The current
  spec deliberately excludes any `.db` — that exclusion exists to stop a *live*
  database being packaged by accident, not to prevent shipping a prepared one.
  A separate, explicitly-named seed file sidesteps it.
- **First-run import.** The app offers to point at an existing `aivionics.db`
  or to run the ingest. Smaller download, more steps for the user.
- **`AIVIONICS_DATA` already works** as a manual escape hatch, but nobody
  should need an environment variable to see their own data.

Whatever is chosen, **first run must not be a blank screen.** If there is
genuinely no data, every screen should say what to do about it, and Home should
lead with that rather than with empty tiles.

## 2. It looks and feels unfinished

> *"It looks/feels unfinished"*

Treat as a full pass over every screen **as a user, not as a developer** — open
it, use it, and fix what feels wrong, rather than checking each screen renders.
The empty states, spacing, and the transitions between screens are where this
shows.

## 3. No minimise animation

> *"there is no minimizing animation"*

The shell and the login are frameless, and a frameless window loses the
system's minimise/restore animation — it vanishes and reappears. The fix is to
keep the native animation, which usually means letting Windows own the frame
(DWM) rather than drawing it ourselves, or restoring the window class flags
that DWM animates.

## 4. The side rail needs names and a hide button

> *"Side panel also must show section names and it must have hide button"*

Today the rail is icons only, with the name in a tooltip. Wanted:
- section names beside the icons,
- a control to collapse it back to icons-only.

So: an expanded rail is the default, collapsing is a user choice, and the
choice persists. `Rail` in `src/aivionics/ui/widgets.py`.

## 5. Question the online/offline switch

> *"Why it is online offline"*

**This is a question about a design decision, not a bug — do not silently
change it, and do not silently defend it.** The switch exists because standing
rule 11/12 requires the manuals core, retrieval, case base and statistics to
run identically with the cable out, with everything network-dependent isolated
behind one setting an IT department can audit.

What is worth re-examining is whether the *user* should ever see that switch.
Reasonable outcomes: keep it but stop making it a visible mode, default it on
and only surface it when a fetch fails, or let each online feature ask for
itself. Bring the options and the trade-off; let the owner decide.

## 6. "and etc."

The list above is not complete. Expect more once the app is used against real
data — see item 1, since most of the feel of the thing is unreachable while it
opens empty.

---

*The engineering backlog (retrieval quality, corpus repair, gold-set
adjudication) is unchanged and lives in `docs/PLAN.md` and
`docs/status/gate2-report.md`. This file is only the owner's product feedback.*
