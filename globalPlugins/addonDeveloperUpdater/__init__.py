from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import addonHandler, config, globalPluginHandler, globalVars, gui, scriptHandler, ui, wx
from gui.settingsDialogs import SettingsPanel
from logHandler import log
from . import engine

addonHandler.initTranslation()
SCRIPT_CATEGORY = _("Add-on Developer Updater")
config.conf.spec["addonDeveloperUpdater"] = {
    "automaticChecks": "boolean(default=True)",
    # Accept legacy 5-minute values so startup can migrate them to the new 15-minute minimum.
    "intervalMinutes": "integer(default=30,min=5,max=1440)",
    "includePrereleases": "boolean(default=True)",
    "automaticManifestUpdates": "boolean(default=False)",
    "scanFixedDrives": "boolean(default=False)",
    "scanRoots": "string(default='')",
    "periodicRescanHours": "integer(default=24,min=1,max=720)",
}

class UpdaterSettingsPanel(SettingsPanel):
    title = SCRIPT_CATEGORY
    def makeSettings(self, settingsSizer):
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer); settings = config.conf["addonDeveloperUpdater"]
        self.automatic = helper.addItem(wx.CheckBox(self, label=_("Check automatically in the background"))); self.automatic.SetValue(settings["automaticChecks"])
        self.prereleases = helper.addItem(wx.CheckBox(self, label=_("Include beta and release candidate builds"))); self.prereleases.SetValue(settings["includePrereleases"])
        self.apply = helper.addItem(wx.CheckBox(self, label=_("Automatically change approved project manifests (advanced)"))); self.apply.SetValue(settings["automaticManifestUpdates"])
        self.fixed = helper.addItem(wx.CheckBox(self, label=_("Include all accessible drives during manual scans"))); self.fixed.SetValue(settings["scanFixedDrives"])
        self.interval = helper.addLabeledControl(_("Release check interval in minutes:"), wx.SpinCtrl, min=15, max=1440, initial=max(15, settings["intervalMinutes"]))
        self.rescan = helper.addLabeledControl(_("Rescan approved folders every number of hours:"), wx.SpinCtrl, min=1, max=720, initial=settings["periodicRescanHours"])
        self.roots = helper.addLabeledControl(_("Additional development folders, separated by semicolons:"), wx.TextCtrl, value=settings["scanRoots"])
    def onSave(self):
        settings = config.conf["addonDeveloperUpdater"]
        settings["automaticChecks"] = self.automatic.IsChecked(); settings["includePrereleases"] = self.prereleases.IsChecked()
        settings["automaticManifestUpdates"] = self.apply.IsChecked(); settings["scanFixedDrives"] = self.fixed.IsChecked()
        settings["intervalMinutes"] = self.interval.GetValue(); settings["periodicRescanHours"] = self.rescan.GetValue(); settings["scanRoots"] = self.roots.GetValue()

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    def __init__(self):
        super().__init__(); self._shutdown = threading.Event(); self._scanCancel = threading.Event(); self._wakeMonitor = threading.Event(); self._scanLock = threading.Lock()
        config_path = Path(globalVars.appArgs.configPath); self._statePath = config_path / "addonDeveloperUpdaterState.json"; self._releaseCachePath = config_path / "addonDeveloperUpdaterReleaseCache.json"; self._backupRoot = config_path / "addonDeveloperUpdaterBackups"
        if config.conf["addonDeveloperUpdater"]["intervalMinutes"] < 15: config.conf["addonDeveloperUpdater"]["intervalMinutes"] = 15
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(UpdaterSettingsPanel)
        self._monitor = threading.Thread(target=self._monitorLoop, name="addonDeveloperUpdater", daemon=True); self._monitor.start()
    def terminate(self):
        self._shutdown.set(); self._scanCancel.set(); self._wakeMonitor.set()
        try: gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(UpdaterSettingsPanel)
        except ValueError: pass
        super().terminate()
    @scriptHandler.script(description=_("Check approved NVDA add-on projects for compatibility updates"), gesture="kb:NVDA+alt+shift+u", category=SCRIPT_CATEGORY)
    def script_checkAddonProjects(self, gesture): self._startCheck(manual=True, full_system=bool(config.conf["addonDeveloperUpdater"]["scanFixedDrives"]))
    @scriptHandler.script(description=_("Discover NVDA add-on projects across all accessible drives"), category=SCRIPT_CATEGORY)
    def script_discoverAddonProjects(self, gesture): self._startCheck(manual=True, full_system=True)
    @scriptHandler.script(description=_("Cancel the running add-on developer update scan"), category=SCRIPT_CATEGORY)
    def script_cancelAddonProjectScan(self, gesture): self._scanCancel.set(); ui.message(_("Add-on developer scan cancellation requested"))
    def _monitorLoop(self):
        while not self._shutdown.is_set():
            settings = config.conf["addonDeveloperUpdater"]
            if settings["automaticChecks"]: self._runCheck(manual=False, full_system=False)
            self._wakeMonitor.wait(max(900, int(settings["intervalMinutes"]) * 60)); self._wakeMonitor.clear()
    def _startCheck(self, manual=False, full_system=False):
        if self._scanLock.locked(): ui.message(_("An add-on developer scan is already running")); return
        self._scanCancel.clear(); ui.message(_("Checking for NVDA releases and add-on projects"))
        threading.Thread(target=self._runCheck, kwargs={"manual": manual, "full_system": full_system}, name="addonDeveloperManualScan", daemon=True).start()
    def _roots(self, full_system=False):
        settings = config.conf["addonDeveloperUpdater"]; roots = list(engine.default_development_roots())
        roots.extend(Path(value.strip()).expanduser() for value in settings["scanRoots"].split(";") if value.strip())
        if full_system: roots.extend(engine.drive_roots())
        unique = {}
        for root in roots:
            try:
                if root.exists(): unique[str(root.resolve()).lower()] = root
            except OSError: pass
        return list(unique.values())
    @staticmethod
    def _rescanDue(state, hours):
        try: return (datetime.now(timezone.utc) - datetime.fromisoformat(state["lastScanAt"])).total_seconds() >= hours * 3600
        except (KeyError, TypeError, ValueError): return True
    def _runCheck(self, manual=False, full_system=False):
        if not self._scanLock.acquire(blocking=False): return
        self._scanCancel.clear(); cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set()
        try:
            settings = config.conf["addonDeveloperUpdater"]; release = engine.latest_release(settings["includePrereleases"], self._releaseCachePath); state = engine.read_json(self._statePath, {})
            if not manual and state.get("releaseTag") == release.tag and not self._rescanDue(state, settings["periodicRescanHours"]): return
            wx.CallAfter(ui.message, _("NVDA %s was found. Scanning approved development folders.") % release.tag)
            manifests = engine.discover_manifests(self._roots(full_system), cancelled=cancelled)
            if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
            approved = set(state.get("approvedProjects", []))
            if not approved:
                approved.update(item.get("project_id") or engine.project_id(Path(item["path"])) for item in state.get("projects", []) if item.get("path"))
            if manual: approved.update(engine.project_id(path) for path in manifests)
            results = []
            for path in manifests:
                if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
                identifier = engine.project_id(path)
                if identifier not in approved:
                    metadata = engine.values(path); results.append(engine.ProjectResult(identifier, metadata.get("name", path.parent.name), str(path), "awaiting manual approval")); continue
                results.append(engine.update(path, release, self._backupRoot, bool(settings["automaticManifestUpdates"]), cancelled))
            counts = {
                "updated": sum(r.status.startswith("updated") for r in results),
                "available": sum(r.status.startswith("update available") for r in results),
                "awaiting": sum("awaiting" in r.status for r in results),
                "failed": sum("failed" in r.status for r in results),
            }
            engine.atomic_json_write(self._statePath, {"releaseTag": release.tag, "manifestVersion": release.manifest_version, "lastScanAt": engine.utc_now(), "approvedProjects": sorted(approved), "projects": [r.__dict__ for r in results]})
            wx.CallAfter(ui.message, _("Scan complete: %d projects, %d updated, %d updates available, %d awaiting approval, %d failed.") % (len(results), counts["updated"], counts["available"], counts["awaiting"], counts["failed"]))
        except Exception:
            log.exception("Add-on Developer Updater check failed"); wx.CallAfter(ui.message, _("The add-on developer update check failed. See the NVDA log for details."))
        finally: self._scanLock.release()
