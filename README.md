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

## Optional attoDRY Python API

For a published ZMeter release containing `devices/attodry`, environment setup
asks whether you will use attoDRY. Answer **no** (the default) to create/update
the normal environment without that API. You can rerun setup to add it later.

If you answer **yes**, keep the vendor-supplied
`api_atto-device-python_1.0.12.whl` beside the installer, retain its
`Attocube/u drive/Software/API/` tree, or enter the wheel's full path when asked.
The historical vendor filename contains a different version spelling; the
package inside is `atto_device` **1.12.0**. The tool verifies SHA-256
`b1c0a7504e532105a5936bdd140054de7d1d2338b322bf12c21b94a7b9a8c5e6`
and stages `atto_device-1.12.0-py3-none-any.whl` without altering your original.
The vendor wheel is not redistributed in this repository.

Verification/cancellation happens before Conda changes. After Conda succeeds,
the optional install uses `conda run --name <selected-release-environment>` and
local pip installation with `--no-index --no-deps`. It does not import the API,
discover controllers, change device configuration or install OS drivers. If
Conda/pip fails, the environment may be partially changed; inspect the output.
No rollback or successful completion is claimed after failure.

## Development branch and tests

`MS/optional-attodry-installation` accompanies ZMeter's
`MS/lab-devices-and-alignment` branch. These are development contributions, not
published releases. The installer still accepts only verified packaged releases;
it does not install moving branch heads or silently label them as beta.4.

Hardware-independent tests use synthetic wheel bytes and mocked subprocesses:

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B -m py_compile deploy_zmeter.py
```

Fourteen tests cover opt-in/skip, file search/path entry, staging/hash failures,
exact environment selection, and Conda/pip failure propagation. Actual Windows
Conda creation/update and vendor-package installation remain user validation.
