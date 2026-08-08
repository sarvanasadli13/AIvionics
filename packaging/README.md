# Packaging and operations

Phase 6 of `docs/PLAN.md`. Build, sign, install, back up — and what to do when
the tool is unavailable.

---

## 1. Build

```powershell
pip install pyinstaller
pyinstaller packaging\aivionics.spec --noconfirm      # -> dist\AIvionics\
iscc packaging\installer.iss                          # -> dist\installer\
```

Pass a version to the installer explicitly so it matches the wheel metadata:

```powershell
iscc /DAppVersion=0.1.0 packaging\installer.iss
```

**One-folder, not one-file.** A one-file build unpacks itself to a temporary
directory on every launch, which costs seconds with PySide6 and is the pattern
endpoint protection flags hardest. One-folder also lets IT see what ships.

**UPX is off deliberately.** Executable compression is itself an AV heuristic;
the space it saves is not worth the false positives it buys.

**The database is not bundled.** It is built on site by the ingest scripts and
lives under `%LOCALAPPDATA%\AIvionics\data`. Shipping a copy would put a stale
corpus inside the install directory where nobody would think to look at it, and
an upgrade would appear to silently change the data.

---

## 2. Code signing — required, and not yet done

**An unsigned PyInstaller binary will be flagged.** This is close to certain on
a managed corporate machine: SmartScreen will warn on first run, and EDR
products treat an unsigned, newly-seen executable that loads a large native
payload as suspicious by default. PLAN §6 budgets for this; it is not optional
polish.

No certificate has been obtained, so **nothing in this repository is signed.**
When one exists (an OV or EV code-signing certificate from a public CA — EV
gets SmartScreen reputation immediately, OV accumulates it):

```powershell
# sign the executable and every DLL PyInstaller collected
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f cert.pfx /p $env:CERT_PASSWORD dist\AIvionics\AIvionics.exe

# then the installer itself, after iscc has produced it
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f cert.pfx /p $env:CERT_PASSWORD dist\installer\AIvionics-0.1.0-setup.exe

signtool verify /pa /v dist\installer\AIvionics-0.1.0-setup.exe
```

Timestamping (`/tr`) is what keeps already-installed copies trusted after the
certificate expires. Do not skip it.

Until the build is signed, expect to walk IT through a SmartScreen warning, and
do not treat an AV detection as evidence of a defect in the application.

---

## 3. Install layout

Everything is per-user. `PrivilegesRequired=lowest`, no service, no registry
key outside `HKCU`, nothing in Program Files.

| Path | Holds |
|---|---|
| `%LOCALAPPDATA%\AIvionics\` | the application |
| `%LOCALAPPDATA%\AIvionics\data\` | `aivionics.db`, the index, the repaired chapter PDFs |
| `%LOCALAPPDATA%\AIvionics\data\backups\` | `VACUUM INTO` snapshots |

The installer creates the data directories and never writes into them again.
**The uninstaller does not remove them** — an uninstall must not take the
department's case base with it.

---

## 4. Backups

```powershell
python scripts\backup.py                    # into data\backups\, then verifies
python scripts\backup.py --out D:\backups
python scripts\backup.py --restore data\backups\aivionics-20260808-173005.db --force
```

**Never copy a live `.db` file.** In WAL mode the database is a file plus a
write-ahead log; copying the file alone captures a torn state that restores as
corruption, and copying both without a checkpoint captures them at different
instants. `VACUUM INTO` asks SQLite for a consistent snapshot.

Every backup is opened, integrity-checked and row-counted against the source
before it is reported as successful — `scripts\backup.py` exits non-zero if any
of that fails. A backup nobody has opened is a belief, not a backup.

`restore` refuses a backup that fails its integrity check, because restoring a
corrupt snapshot over a working database turns a recoverable situation into an
unrecoverable one. It also removes the stale `-wal`/`-shm` sidecars of the file
it replaces, which would otherwise be reapplied to a database they no longer
match.

3-2-1: the `data\backups` copy is one. Put a second on other media and a third
off site; neither is automated here.

---

## 5. Health checks

Run at startup and reported in Admin:

- `PRAGMA integrity_check` on the database
- audit hash-chain verification (`audit.verify_chain`) — a broken chain means
  rows were altered outside the application
- version stamps for application, schema, index and every model

**Changing the embedding model invalidates every stored vector and every
measurement taken with it** (standing rule 9). `config.INDEX_VERSION` must be
bumped whenever `EMBED_MODEL` changes, and the index rebuilt; the versions
report shows all of it together so a mismatch is visible rather than inferred.

---

## 6. When the tool is unavailable

Stated plainly because the answer must not be improvised at the moment it is
needed.

**AIvionics is decision support. It is not part of the official maintenance
record, and nothing in it is a source of maintenance data.** If the application
will not start, the index is missing, the database fails its integrity check,
or any result looks wrong:

1. **Use the approved source directly** — the controlled manual for procedures,
   the CAMO for compliance status. Neither depends on this tool, and both
   remain the legal record whether it is running or not.
2. Report the failure with the version block from Admin (or
   `python scripts\backup.py`, which prints it).
3. Restore the most recent verified backup only if the case base is the
   problem. The manual corpus is rebuildable from the source PDFs with
   `scripts\phase1.py` and does not need a backup to recover.

No work stops because this application is down. That is a property of the
design — it indexes into controlled data rather than replacing it — and it is
the reason the locator-only rule exists.
