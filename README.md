# ZMeter Deployment Tool

A lightweight Windows installer for published [ZMeter](https://github.com/lictailer/zmeter) releases.

## Use

Double-click `deploy_zmeter.py`, then choose whether to install ZMeter or create/update its release-specific Conda environment. The installer verifies the selected release and shows download, extraction, or Conda progress before it finishes.

Requirements: Windows, Python 3.12, Conda, and an internet connection. The tool does not launch ZMeter or access hardware.

For a non-modifying check, run:

```powershell
python deploy_zmeter.py --preview
```

An optional offline release catalog can be previewed with `python deploy_zmeter.py --catalog <path>`. Catalog mode never installs or changes anything.
