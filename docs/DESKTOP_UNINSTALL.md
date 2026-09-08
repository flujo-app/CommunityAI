# Remove CommunityAI or reclaim its downloaded models

The alpha installers preserve your settings, credentials and downloaded models.
Reinstalling the application can reuse them. Deleting downloads is an explicit
manual choice in this alpha; the uninstaller does not offer an automatic data
deletion checkbox.

## Before removing the application

1. Open **Sharing** and turn off **Start CommunityAI when I sign in**. Confirm
   that the status below it says **Off**. The alpha uninstaller does not remove
   an existing per-user login entry.
2. Pause sharing, then close the CommunityAI window. The installer also performs
   a shutdown check and refuses maintenance if shutdown fails.
3. Choose which data to keep below. Perform any credential reset while the
   installed application is still available, then uninstall.

On Windows, use **Settings → Apps → CommunityAI → Uninstall** or its Start-menu
uninstall shortcut. On Ubuntu/Debian, run `sudo apt remove communityai`.
Package removal, including `apt purge`, does not delete files in your home
directory.

## Keep everything for a later reinstall

Uninstall after the steps above. Your resource preferences, model policy, API
keys, peer identity and verified downloads remain. Install the next setup or
`.deb` to use the retained state. Updates also preserve these files.

## Delete downloaded models but keep settings

After CommunityAI has stopped, open your home directory and review the exact
managed cache folder:

| Platform | Default managed model cache |
| --- | --- |
| Windows | `%USERPROFILE%\.drift\node\model-cache` |
| Ubuntu/Debian | `~/.drift/node/model-cache` |

Delete only that **model-cache** folder if you want to reclaim its disk space.
Keep its parent **node** folder and `node-config.json` to retain your settings.
This removes local model and sharing-worker downloads; using those models again
requires downloading and verifying the missing artifacts.

If you have changed cache locations or imported an existing node configuration,
review the `cache_dir` values in `node-config.json` first. Each model or worker
may use another directory. Delete a custom directory only after checking that it
contains solely model data you want removed. The legacy `DRIFT_CACHE` location
and `~/.cache/drift` can be shared with other Drift installations. Removing the
managed folder does not clear those other locations.

## Reset this user's CommunityAI settings and credentials

This is a separate choice from freeing downloaded models. It discards resource
preferences, API keys, peer identity, catalog state and downloads stored under
the default node directory. Existing local API integrations will need new keys
after setup.

First stop CommunityAI and disable login startup as described above. While it
is still installed, remove its native control credential:

Windows PowerShell, for the default installation location:

```powershell
& "$env:LOCALAPPDATA\Programs\CommunityAI\CommunityAI.exe" --delete-control-key
```

Ubuntu/Debian, as your ordinary desktop user:

```sh
communityai --delete-control-key
```

Then delete the exact `%USERPROFILE%\.drift\node` folder on Windows or
`~/.drift/node` folder on Linux, after reviewing its contents. Do not delete the
whole `.drift` directory if another Drift installation uses it. If you imported
headless state, used custom paths or intentionally share this node with another
client, review that setup before deleting its state or credential.

Uninstall the application when finished. Reinstallation starts fresh and needs
new API keys and new downloads. Normal filesystem deletion is not a secure erase
of recoverable disk blocks or backups.

## If you already uninstalled with login startup enabled

Reinstall in the same location, open Sharing and disable the sign-in toggle,
then uninstall again. This is the supported manual cleanup path for the alpha.
An advanced user can instead remove only the corresponding **CommunityAI**
entry from their Windows per-user Run key or
`~/.config/autostart/communityai.desktop` on Linux after checking its command.
On Linux, `XDG_CONFIG_HOME` can place the autostart file elsewhere.

The [Gate 15 choice evidence](evidence/gate15-20260908-windows-data-login-choices.json)
records the tested scope. The separate installer lifecycle evidence proves
settings/cache retention during actual replacement and removal.
