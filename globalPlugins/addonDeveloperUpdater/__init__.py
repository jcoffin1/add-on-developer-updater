from __future__ import annotations

import json
import threading
from pathlib import Path

import addonHandler
import config
import globalPluginHandler
import globalVars
import gui
import scriptHandler
import ui
import wx
from gui.settingsDialogs import SettingsPanel
from logHandler import log

from . import engine

addonHandler.initTranslation()
SCRIPT_CATEGORY = _("Add-on Developer Updater")

config.conf.spec["addonDeveloperUpdater"] = {
    "automaticChecks": "boolean(default=True)",
    "intervalMinutes": "integer(default=15,min=5,max=1440)",
    "includePrereleases": "boolean(default=True)",
    "automaticManifestUpdates": "boolean(default=True)",
    "scanFixedDrives": "boolean(default=True)",
}


class UpdaterSettingsPanel(SettingsPanel):
    title = SCRIPT_CATEGORY

    def makeSettings(self, settingsSizer):
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
        settings = config.conf["addonDeveloperUpdater"]
        self.automatic = helper.addItem(wx.CheckBox(self, label=_("Check automatically in the background")))
        self.automatic.SetValue(settings["automaticChecks"])
        self.prereleases = helper.addItem(wx.CheckBox(self, label=_("Include beta and release candidate builds")))
        self.prereleases.SetValue(settings["includePrereleases"])
        self.apply = helper.addItem(wx.CheckBox(self, label=_("Update validated manifests automatically")))
        self.apply.SetValue(settings["automaticManifestUpdates"])
        self.fixed = helper.addItem(wx.CheckBox(self, label=_("Scan accessible fixed and removable drives")))
        self.fixed.SetValue(settings["scanFixedDrives"])
        self.interval = helper.addLabeledControl(_("Check interval in minutes:"), wx.SpinCtrl, min=5, max=1440, initial=settings["intervalMinutes"])

    def onSave(self):
        settings = config.conf["addonDeveloperUpdater"]
        settings["automaticChecks"] = self.automatic.IsChecked()
        settings["includePrereleases"] = self.prereleases.IsChecked()
        settings["automaticManifestUpdates"] = self.apply.IsChecked()
        settings["scanFixedDrives"] = self.fixed.IsChecked()
        settings["intervalMinutes"] = self.interval.GetValue()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    def __init__(self):
        super().__init__()
        self._shutdown = threading.Event()
        self._scanCancel = threading.Event()
        self._scanLock = threading.Lock()
        self._statePath = Path(globalVars.appArgs.configPath) / "addonDeveloperUpdaterState.json"
        self._backupRoot = Path(globalVars.appArgs.configPath) / "addonDeveloperUpdaterBackups"
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(UpdaterSettingsPanel)
        self._monitor = threading.Thread(target=self._monitorLoop, name="addonDeveloperUpdater", daemon=True)
        self._monitor.start()

    def terminate(self):
        self._shutdown.set()
        self._scanCancel.set()
        try:
            gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(UpdaterSettingsPanel)
        except ValueError:
            pass
        super().terminate()

    @scriptHandler.script(
        description=_("Check this system and synchronized cloud storage for NVDA add-on compatibility updates"),
        gesture="kb:NVDA+alt+shift+u",
        category=SCRIPT_CATEGORY,
    )
    def script_checkAddonProjects(self, gesture):
        self._startCheck(manual=True)

    @scriptHandler.script(
        description=_("Cancel the running add-on developer update scan"),
        category=SCRIPT_CATEGORY,
    )
    def script_cancelAddonProjectScan(self, gesture):
        self._scanCancel.set()
        ui.message(_("Add-on developer scan cancellation requested"))

    def _monitorLoop(self):
        while not self._shutdown.is_set():
            settings = config.conf["addonDeveloperUpdater"]
            if settings["automaticChecks"]:
                self._runCheck(manual=False)
            self._shutdown.wait(max(300, int(settings["intervalMinutes"]) * 60))

    def _startCheck(self, manual=False):
        if self._scanLock.locked():
            ui.message(_("An add-on developer scan is already running"))
            return
        self._scanCancel.clear()
        ui.message(_("Checking for NVDA releases and add-on projects"))
        threading.Thread(target=self._runCheck, kwargs={"manual": manual}, name="addonDeveloperManualScan", daemon=True).start()

    def _roots(self):
        roots = engine.cloud_roots()
        if config.conf["addonDeveloperUpdater"]["scanFixedDrives"]:
            roots.extend(engine.drive_roots())
        unique = {}
        for root in roots:
            unique[str(root).lower()] = root
        return list(unique.values())

    def _runCheck(self, manual=False):
        if not self._scanLock.acquire(blocking=False):
            return
        try:
            settings = config.conf["addonDeveloperUpdater"]
            release = engine.latest_release(settings["includePrereleases"])
            state = {}
            if self._statePath.exists():
                try:
                    state = json.loads(self._statePath.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not manual and state.get("releaseTag") == release.tag:
                return
            wx.CallAfter(ui.message, _("NVDA %s was found. Scanning add-on development projects.") % release.tag)
            manifests = engine.discover_manifests(self._roots(), cancelled=lambda: self._scanCancel.is_set() or self._shutdown.is_set())
            results = [engine.update(path, release, self._backupRoot, settings["automaticManifestUpdates"]) for path in manifests]
            updated = sum(result.status.startswith("updated") for result in results)
            failed = sum("failed" in result.status for result in results)
            state = {"releaseTag": release.tag, "manifestVersion": release.manifest_version, "projects": [result.__dict__ for result in results]}
            self._statePath.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            message = _("Scan complete: %d projects found, %d updated, %d need attention.") % (len(results), updated, failed)
            wx.CallAfter(ui.message, message)
        except Exception:
            log.exception("Add-on Developer Updater check failed")
            wx.CallAfter(ui.message, _("The add-on developer update check failed. See the NVDA log for details."))
        finally:
            self._scanLock.release()
