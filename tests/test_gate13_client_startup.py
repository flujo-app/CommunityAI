import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "gate13_windows_client_startup.ps1"
LINUX = ROOT / "scripts" / "gate13_linux_client_startup.sh"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")


def test_windows_bootstrap_preserves_the_proven_interactive_boundary():
    source = WINDOWS.read_text(encoding="utf-8")

    assert "RandomNumberGenerator]::Create()" in source
    assert "RandomNumberGenerator]::Fill" not in source
    assert 'if ((Get-Service -Name sshd).Status -ne "Running") { Start-Service -Name sshd }' in source
    assert source.count('Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Enabled True -Profile Any') == 2
    assert 'New-LocalUser -Name "M"' in source
    assert 'Remove-LocalGroupMember -Group "Administrators" -Member "M"' in source
    assert 'New-LocalUser -Name "Gate13Admin"' in source
    assert 'Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value "1"' in source
    assert 'New-ScheduledTaskTrigger -AtLogOn -User "M"' in source
    assert "Remove-ItemProperty -Path $p -Name DefaultPassword" in source
    assert "Restart-Computer -Force" in source
    assert "2a52993092a19cfdffe126e2eeac46a4265e25705614546604ad44988e040c0f" in source
    assert "communityai_gate13_m_authorized_keys" in source


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell parser")
def test_windows_bootstrap_parses_natively():
    probe = (
        f"$source=Get-Content -Raw -LiteralPath '{WINDOWS}';"
        "$tokens=$null;$errors=$null;"
        "[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count -ne 0){$errors|ForEach-Object{$_.Message};exit 2}"
    )
    result = subprocess.run(
        [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            probe,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_linux_bootstrap_contains_the_proven_x11_runtime_and_display():
    source = LINUX.read_text(encoding="utf-8")
    for package in (
        "xvfb",
        "dbus-x11",
        "gnome-keyring",
        "libsecret-tools",
        "libxcb-cursor0",
        "libxcb-icccm4",
        "libxcb-keysyms1",
        "libxcb-shape0",
        "libxkbcommon-x11-0",
    ):
        assert package in source
    assert "/usr/bin/Xvfb :99" in source
    assert "-nolisten tcp" in source
    assert "DISPLAY=:99 xdpyinfo" in source


def test_linux_bootstrap_bounds_apt_network_and_lock_waits():
    source = LINUX.read_text(encoding="utf-8")

    assert "apt_deadline=$(( $(date +%s) + 300 ))" in source
    assert "for attempt in 1 2 3" in source
    assert '"${attempt_timeout}s"' in source
    assert "Acquire::ForceIPv4=true" in source
    assert "Acquire::Retries=1" in source
    assert "Acquire::http::Timeout=20" in source
    assert "Acquire::https::Timeout=20" in source
    assert "DPkg::Lock::Timeout=30" in source
    assert "update attempt ${attempt}/3" in source
    assert 'apt-get "${apt_options[@]}" install -y' in source
    assert "bootstrap_status=/var/lib/gate13-bootstrap-status" in source


@pytest.mark.skipif(BASH is None, reason="requires bash parser")
def test_linux_bootstrap_parses_natively():
    result = subprocess.run(
        [BASH, "-n", str(LINUX)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
