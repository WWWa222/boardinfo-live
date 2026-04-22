# TaishanPi Boardinfo Live

Small TaishanPi telemetry installer and terminal dashboard.

The installer reads SSH connection details from the parent project's
`config/ssh.txt`, uploads two scripts to the board, and installs them as:

```sh
/usr/local/bin/boardinfo
/usr/local/bin/boardinfo-live
```

## Usage

From the parent project directory:

```powershell
python .\boardinfo-live\install_boardinfo.py --once
python .\boardinfo-live\install_boardinfo.py --live-once
```

On the board:

```sh
boardinfo
boardinfo-live
```

`boardinfo-live` is an interactive curses dashboard:

- `Up` / `Down`: select telemetry item
- `q`: quit
- left panel: current values
- right panel: selected item's fixed-scale area chart

## Notes

This repository intentionally does not include SSH credentials, board images,
logs, generated archives, or local project configuration.
