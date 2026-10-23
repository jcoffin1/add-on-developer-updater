from __future__ import annotations

import os, subprocess, threading, uuid
from datetime import datetime
from pathlib import Path

import addonHandler, api, config, globalPluginHandler, globalVars, gui, scriptHandler, tones, ui, wx
from gui.settingsDialogs import SettingsPanel
from logHandler import log
from . import engine, publisher
from .dialogs import (
    _projectDisplayName, UpdateReviewDialog, ProjectApprovalDialog, GitHubPublishDialog, StoreSubmissionDialog, GitHubRepositoryUrlDialog, GitHubSingleSelectionDialog, IssueTemplateEditorDialog, CompatibilityTargetDialog, ScanTargetDialog, ConfirmationDialog, InformationDialog, OperationProgressDialog,
)

addonHandler.initTranslation()
SCRIPT_CATEGORY = _("Add-on Developer Updater")
config.conf.spec["addonDeveloperUpdater"] = {
    "automaticChecks": "boolean(default=False)",
    # Accept legacy 5-minute values so startup can migrate them to the new 15-minute minimum.
    "intervalMinutes": "integer(default=30,min=5,max=1440)",
    "includePrereleases": "boolean(default=True)",
    "scanFixedDrives": "boolean(default=False)",
    "scanRoots": "string(default='')",
    "periodicRescanHours": "integer(default=24,min=1,max=720)",
    # Blank values use each manifest's author and the project's license file.
    "storePublisher": "string(default='')",
    "storeFallbackLicenseName": "string(default='')",
    "storeFallbackLicenseUrl": "string(default='')",
    "notificationSound": "boolean(default=True)",
}

class UpdaterSettingsPanel(SettingsPanel):
    title = SCRIPT_CATEGORY
    def makeSettings(self, settingsSizer):
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer); settings = config.conf["addonDeveloperUpdater"]
        self.automatic = helper.addItem(wx.CheckBox(self, label=_("Check automatically in the background"))); self.automatic.SetValue(settings["automaticChecks"])
        self.prereleases = helper.addItem(wx.CheckBox(self, label=_("Include alpha, beta, and release candidate builds"))); self.prereleases.SetValue(settings["includePrereleases"])
        self.fixed = helper.addItem(wx.CheckBox(self, label=_("Include all accessible drives during manual scans"))); self.fixed.SetValue(settings["scanFixedDrives"])
        self.interval = helper.addLabeledControl(_("Release check interval in minutes:"), wx.SpinCtrl, min=15, max=1440, initial=max(15, settings["intervalMinutes"]))
        self.rescan = helper.addLabeledControl(_("Recheck tracked add-ons every number of hours:"), wx.SpinCtrl, min=1, max=720, initial=settings["periodicRescanHours"])
        self.roots = helper.addLabeledControl(_("Additional development folders, separated by semicolons:"), wx.TextCtrl, value=settings["scanRoots"])
        # Labels are short because NVDA reads the whole label each time a field gains focus.
        self.storePublisher = helper.addLabeledControl(_("Store publisher name (blank uses the manifest author):"), wx.TextCtrl, value=settings["storePublisher"])
        self.storeLicenseName = helper.addLabeledControl(_("Store license name if no license file is found:"), wx.TextCtrl, value=settings["storeFallbackLicenseName"])
        self.storeLicenseUrl = helper.addLabeledControl(_("Store license web address if no license file is found:"), wx.TextCtrl, value=settings["storeFallbackLicenseUrl"])
        self.notificationSound = helper.addItem(wx.CheckBox(self, label=_("Play a sound when an automatic check finds something new"))); self.notificationSound.SetValue(settings["notificationSound"])
    def onSave(self):
        settings = config.conf["addonDeveloperUpdater"]
        settings["automaticChecks"] = self.automatic.IsChecked(); settings["includePrereleases"] = self.prereleases.IsChecked()
        settings["scanFixedDrives"] = self.fixed.IsChecked()
        settings["intervalMinutes"] = self.interval.GetValue(); settings["periodicRescanHours"] = self.rescan.GetValue(); settings["scanRoots"] = self.roots.GetValue()
        settings["storePublisher"] = self.storePublisher.GetValue().strip(); settings["storeFallbackLicenseName"] = self.storeLicenseName.GetValue().strip(); settings["storeFallbackLicenseUrl"] = self.storeLicenseUrl.GetValue().strip(); settings["notificationSound"] = self.notificationSound.IsChecked()
        if GlobalPlugin.activeInstance is not None: GlobalPlugin.activeInstance._settingsChanged()

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    activeInstance = None
    def __init__(self):
        super().__init__(); self._shutdown = threading.Event(); self._scanCancel = threading.Event(); self._scanActive = threading.Event(); self._wakeMonitor = threading.Event(); self._scanLock = threading.Lock(); self._progressStop = threading.Event(); self._progressThread = None; self._manifestThread = None; self._progressDialog = None; self._progressValue = 0; self._progressDeterminate = False; self._scanTargetDialog = None; self._reviewDialog = None; self._approvalDialog = None; self._publishDialog = None; self._storeDialog = None; self._repositoryDialog = None; self._githubSelectionDialog = None; self._githubSelectionAction = None; self._templateEditorDialog = None; self._downgradeDialog = None; self._confirmDialog = None; self._informationDialog = None; self._workerProcess = None
        config_path = Path(globalVars.appArgs.configPath); self._statePath = config_path / "addonDeveloperUpdaterState.json"; self._releaseCachePath = config_path / "addonDeveloperUpdaterReleaseCache.json"; self._apiVersionCachePath = config_path / "addonDeveloperUpdaterApiVersions.json"; self._backupRoot = config_path / "addonDeveloperUpdaterBackups"
        if config.conf["addonDeveloperUpdater"]["intervalMinutes"] < 15: config.conf["addonDeveloperUpdater"]["intervalMinutes"] = 15
        if UpdaterSettingsPanel not in gui.settingsDialogs.NVDASettingsDialog.categoryClasses: gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(UpdaterSettingsPanel)
        GlobalPlugin.activeInstance = self
        self._monitor = None
        if config.conf["addonDeveloperUpdater"]["automaticChecks"]: self._startMonitor()
    def _startMonitor(self):
        if self._monitor is not None and self._monitor.is_alive(): return
        self._monitor = threading.Thread(target=self._monitorLoop, name="addonDeveloperUpdater", daemon=True); self._monitor.start()
    def _settingsChanged(self):
        if config.conf["addonDeveloperUpdater"]["automaticChecks"]: self._startMonitor()
        self._wakeMonitor.set()
    def _startProgress(self, keepFocus=False):
        # No activity pulse: an animated percentage made NVDA beep or speak numbers
        # that did not measure progress.  Real step counts show the bar instead.
        self._stopProgress(); stopEvent = threading.Event(); self._progressStop = stopEvent; self._progressValue = 0; self._progressDeterminate = False; wx.CallAfter(self._showProgress, stopEvent, keepFocus)
    def _showProgress(self, stopEvent, keepFocus=False):
        if stopEvent.is_set() or self._shutdown.is_set(): return
        if self._progressDialog is not None: self._progressDialog.Destroy()
        self._progressDialog = OperationProgressDialog(gui.mainFrame)
        dialog = self._progressDialog
        dialog.Bind(wx.EVT_CLOSE, lambda event, dialog=dialog: self._onProgressClose(event, dialog))
        if keepFocus:
            self._progressDialog.Show(); self._progressDialog.Raise(); self._progressDialog.status.SetFocus()
        else: self._progressDialog.ShowWithoutActivating()
    def _onProgressClose(self, event, dialog):
        if self._progressDialog is dialog:
            self._progressDialog = None
            dialog.Destroy()
            ui.message(_("Progress window closed. The operation is still running in the background."))
            return
        event.Skip()
    def _updateProgressMessage(self, message):
        self._setProgressMessage(message)
        ui.message(_(message))
    def _setProgressMessage(self, message):
        if self._progressDialog is not None:
            self._progressDialog.setStatus(message); self._progressDialog.Layout()
    def _setProgressStep(self, stopEvent, message, current, total):
        if stopEvent is not self._progressStop or stopEvent.is_set(): return
        self._progressDeterminate = True
        if self._progressDialog is not None:
            self._progressValue = min(100, max(0, round(current * 100 / max(1, total)))); self._progressDialog.setStatus(message); self._progressDialog.setProgress(self._progressValue); self._progressDialog.Layout()
    def _stopProgress(self):
        self._progressStop.set(); self._progressThread = None; wx.CallAfter(self._closeProgress)
    def _finishProgressThen(self, callback, *args):
        """Close the active progress window before creating its replacement.

        Destroying a focused wx dialog restores the previous foreground window on a
        later Windows message.  Therefore showing the next dialog first (and then
        destroying progress) can silently strand the new dialog behind Explorer.
        """
        self._progressStop.set(); self._progressThread = None
        wx.CallAfter(self._closeProgressThen, callback, args)
    def _closeProgressThen(self, callback, args):
        self._closeProgress()
        if not self._shutdown.is_set(): wx.CallAfter(callback, *args)
    def _closeProgress(self):
        if self._progressDialog is not None:
            dialog = self._progressDialog; self._progressDialog = None; dialog.Destroy()
        if self._confirmDialog is not None:
            self._confirmDialog.Raise(); self._confirmDialog.message.SetFocus()
        elif self._informationDialog is not None:
            self._informationDialog.Raise(); self._informationDialog.message.SetFocus()
    def terminate(self):
        self._shutdown.set(); self._scanCancel.set(); self._wakeMonitor.set(); self._stopProgress()
        publisher.cancel_running_commands()
        if self._workerProcess is not None:
            try: self._workerProcess.terminate()
            except OSError: pass
        manifestThread = self._manifestThread
        if manifestThread is not None and manifestThread.is_alive() and threading.current_thread() is not manifestThread:
            manifestThread.join(timeout=2)
        if self._reviewDialog is not None:
            self._reviewDialog.Destroy(); self._reviewDialog = None
        if self._scanTargetDialog is not None:
            self._scanTargetDialog.Destroy(); self._scanTargetDialog = None
        if self._approvalDialog is not None:
            self._approvalDialog.Destroy(); self._approvalDialog = None
        if self._publishDialog is not None:
            self._publishDialog.Destroy(); self._publishDialog = None
        if self._storeDialog is not None:
            self._storeDialog.Destroy(); self._storeDialog = None
        if self._repositoryDialog is not None:
            self._repositoryDialog.Destroy(); self._repositoryDialog = None
        if self._githubSelectionDialog is not None:
            self._githubSelectionDialog.Destroy(); self._githubSelectionDialog = None; self._githubSelectionAction = None
        if self._templateEditorDialog is not None:
            self._templateEditorDialog.Destroy(); self._templateEditorDialog = None
        if self._downgradeDialog is not None:
            self._downgradeDialog.Destroy(); self._downgradeDialog = None
        if self._confirmDialog is not None:
            self._confirmDialog.Destroy(); self._confirmDialog = None
        if self._informationDialog is not None:
            self._informationDialog.Destroy(); self._informationDialog = None
        if self._monitor is not None and self._monitor.is_alive() and threading.current_thread() is not self._monitor: self._monitor.join(timeout=2)
        try: gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(UpdaterSettingsPanel)
        except ValueError: pass
        if GlobalPlugin.activeInstance is self: GlobalPlugin.activeInstance = None
        super().terminate()
    @scriptHandler.script(description=_("Discover and save NVDA add-on compatibility updates for later review"), gesture="kb:NVDA+alt+shift+u", category=SCRIPT_CATEGORY)
    def script_checkAddonProjects(self, gesture): self._startCheck(manual=True, full_system=bool(config.conf["addonDeveloperUpdater"]["scanFixedDrives"]))
    @scriptHandler.script(description=_("Discover NVDA add-on projects across all accessible drives"), category=SCRIPT_CATEGORY)
    def script_discoverAddonProjects(self, gesture): self._startCheck(manual=True, full_system=True)
    @scriptHandler.script(description=_("Report add-on update-check status"), category=SCRIPT_CATEGORY)
    def script_reportAddonUpdateCheckStatus(self, gesture):
        state = engine.read_json(self._statePath, {}); records = engine.visible_project_records(state)[0]; approved = engine.saved_approved_project_ids(state)
        available = sum(str(item.get("status", "")).startswith("update available: ") for item in records if isinstance(item, dict)); awaiting = sum("awaiting" in str(item.get("status", "")) for item in records if isinstance(item, dict)); failed = sum("failed" in str(item.get("status", "")) for item in records if isinstance(item, dict)); approvedCount = sum(item.get("project_id") in approved for item in records if isinstance(item, dict))
        source = str(state.get("releaseSource", "unknown")); release = str(state.get("releaseTag", "unknown")); buildVersion = str(state.get("releaseBuildVersion", "")); compatibility = str(state.get("manifestVersion", "unknown"))
        build = release + (f" ({buildVersion})" if buildVersion and buildVersion != release else "")
        # One fact per line, outcome first, so speech starts with what matters and
        # braille users can move line by line to the detail they need.
        attention = [text for count, text in ((available, _("%d updates available") % available), (awaiting, _("%d add-ons found but not tracked yet") % awaiting), (failed, _("%d projects could not be checked") % failed)) if count]
        if attention: summary = ", ".join(attention) + "."
        elif not state.get("lastSuccessfulCheckAt"): summary = _("No check has completed yet. Press NVDA+Alt+Shift+U to run one.")
        else: summary = _("No updates or problems found.")
        details = [
            _("Tracked add-ons: %d") % approvedCount, _("Updates available: %d") % available, _("Found but not tracked yet: %d") % awaiting, _("Could not be checked: %d") % failed,
            _("NVDA build checked: %s") % build, _("Compatibility target: %s") % compatibility, _("Release information from: %s") % source,
            _("Last successful check: %s") % self._formatCheckTime(state.get("lastSuccessfulCheckAt")), _("Last project check: %s") % self._formatCheckTime(state.get("lastScanAt")), _("Last folder search: %s") % self._formatCheckTime(state.get("lastDiscoveryAt")),
        ]
        if state.get("lastDiscoveryDirectories") is not None:
            details.append(_("Folders searched: %d in %s seconds") % (int(state.get("lastDiscoveryDirectories", 0)), state.get("lastDiscoverySeconds", 0)))
            if state.get("lastDiscoveryTruncated"): details.append(self._discoveryLimitText(state.get("lastDiscoveryCompleteDepth")))
        if config.conf["addonDeveloperUpdater"]["automaticChecks"]: details.append(_("Next automatic check: in about %d minutes") % max(1, round(self._nextAutomaticCheckDelay(state, config.conf["addonDeveloperUpdater"]) / 60)))
        else: details.append(_("Automatic checks: off"))
        if state.get("lastCheckError"): details.append(_("Most recent error: %s") % state["lastCheckError"])
        self._showInformation(_("Add-on update-check status"), engine.readable_message(summary, details))
    @staticmethod
    def _formatCheckTime(value):
        try: return datetime.fromisoformat(str(value)).astimezone().strftime("%B %d, %Y at %I:%M %p")
        except (TypeError, ValueError): return _("never")
    @scriptHandler.script(description=_("Choose which found add-on projects to track"), category=SCRIPT_CATEGORY)
    def script_reviewDiscoveredAddonProjects(self, gesture):
        state = engine.read_json(self._statePath, {}); awaiting = []
        for item in engine.visible_project_records(state)[0]:
            if isinstance(item, dict) and "awaiting" in str(item.get("status", "")):
                try: awaiting.append(engine.ProjectResult(**item))
                except TypeError: pass
        if not awaiting: ui.message(_("No found add-ons are waiting to be tracked. Press NVDA+Alt+Shift+U to search again.")); return
        release = engine.Release(str(state.get("releaseTag", "")), str(state.get("manifestVersion", "")), bool(state.get("prerelease")), str(state.get("releaseUrl", "")), str(state.get("releaseSource", "saved")), str(state.get("releaseCheckedAt", "")), str(state.get("releaseBuildVersion", "")))
        self._showProjectApprovalDialog(awaiting, release)
    @scriptHandler.script(description=_("Set an older last tested NVDA version for selected add-ons"), gesture="kb:NVDA+alt+shift+d", category=SCRIPT_CATEGORY)
    def script_downgradeAddonCompatibility(self, gesture):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Loading tracked add-ons and official NVDA compatibility versions")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadCompatibilityTargets, name="addonDeveloperCompatibilityTargets", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start compatibility-target lookup"); ui.message(_("Compatibility targets could not be loaded"))
    def _loadCompatibilityTargets(self):
        callback = None; arguments = ()
        try:
            officialChoices = engine.compatibility_version_choices(publisher.nvda_api_versions(self._apiVersionCachePath))
            if not officialChoices: raise RuntimeError("the official NVDA Add-on Store returned no compatibility versions")
            state = engine.read_json(self._statePath, {}); approved = engine.approved_project_ids(state); records = engine.newest_local_project_records(engine.visible_project_records(state)[0])[0]; projects = []
            for item in records:
                if not isinstance(item, dict) or item.get("project_id") not in approved or not item.get("path"): continue
                try:
                    path = Path(item["path"]); metadata = engine.values(path); branch, changed = engine.git_manifest_state(path)
                    projects.append(engine.CompatibilityTargetProject(item["project_id"], metadata.get("name", item.get("name", path.parent.name)), str(path), metadata["lasttestednvdaversion"], metadata["minimumnvdaversion"], metadata.get("updatechannel", ""), branch, changed))
                except (KeyError, OSError, ValueError):
                    log.exception("Could not load compatibility details for %s", item.get("path"))
            if projects: callback, arguments = self._showDowngradeDialog, (projects, officialChoices)
            else: callback, arguments = self._showInformation, (_("No compatibility targets available"), _("No tracked add-ons are available to change."))
        except Exception as error:
            log.exception("Compatibility-target lookup failed"); callback, arguments = self._showInformation, (_("Compatibility targets could not be loaded"), _("Official NVDA compatibility versions could not be loaded: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showDowngradeDialog(self, projects, officialChoices):
        if self._downgradeDialog is not None: self._downgradeDialog.Raise(); return
        dialog = CompatibilityTargetDialog(gui.mainFrame, projects, officialChoices); self._downgradeDialog = dialog
        def close(_event=None):
            if self._downgradeDialog is dialog: self._downgradeDialog = None
            dialog.Destroy()
        def apply(_event=None):
            selected = dialog.selectedResults(); target = dialog.selectedTarget()
            if not selected: ui.message(_("No add-on projects were selected")); return
            if not target: ui.message(_("The selected add-ons do not share an official older NVDA target")); return
            changes = "; ".join(_("%s: NVDA %s to %s, branch %s") % (item.name, item.current_version, target, item.branch) for item in selected)
            dirty = [item.name for item in selected if item.manifest_changed]
            message = _("Change only lastTestedNVDAVersion for %d manifests? This does not test compatibility or change minimumNVDAVersion, the add-on version, source code, or NVDA. Planned changes: %s.") % (len(selected), changes)
            if dirty: message += _(" Warning: these manifests already have uncommitted changes: %s. Their current contents will be backed up before editing.") % "; ".join(dirty)
            self._showConfirmation(_("Confirm older compatibility target"), message, lambda: (close(), self._startDowngrade(selected, target, dict(officialChoices))))
        dialog.onUpdate = apply; dialog.applyButton.Bind(wx.EVT_BUTTON, apply); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _startDowngrade(self, selected, target, officialVersions):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        self._scanCancel.clear(); self._scanActive.set(); ui.message(_("Setting %d selected manifests to last tested NVDA %s") % (len(selected), target)); self._startProgress()
        try:
            self._manifestThread = threading.Thread(target=self._downgradeSelected, args=(selected, target, officialVersions), name="addonDeveloperCompatibilityChange", daemon=True); self._manifestThread.start()
        except Exception: self._manifestThread = None; self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start compatibility change"); ui.message(_("The compatibility change could not be started"))
    def _downgradeSelected(self, selected, target, officialVersions):
        results = []; saveError = ""; cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set()
        try:
            for item in selected:
                if cancelled(): results.append(engine.ProjectResult(item.project_id, item.name, item.path, "cancelled")); continue
                try: results.append(engine.downgrade(Path(item.path), target, self._backupRoot, officialVersions, cancelled))
                except Exception as error: results.append(engine.ProjectResult(item.project_id, item.name, item.path, f"validation failed: {error}"))
            for result in results: log.info("Add-on Developer Updater compatibility target: %s: %s", result.path, result.status)
            changed = [result for result in results if result.status.startswith("set last tested NVDA from ")]
            state = engine.read_json(self._statePath, {}); state["projects"] = engine.merge_project_records(state.get("projects", []), results)
            if changed:
                state["compatibilityUndo"] = [{"project_id": result.project_id, "name": result.name, "path": result.path, "backup_path": result.backup_path, "previous_last_tested": result.previous_last_tested, "target_last_tested": result.target_last_tested, "target_manifest_hash": result.target_manifest_hash} for result in changed]
            engine.atomic_json_write(self._statePath, state)
        except Exception as error:
            saveError = str(error); log.exception("Compatibility target results could not be saved")
        finally:
            self._scanActive.clear(); self._scanLock.release()
            self._finishProgressThen(self._finishDowngrade, results, saveError)
    def _finishDowngrade(self, results, saveError=""):
        changed = [item for item in results if item.status.startswith("set last tested NVDA from ")]; failed = [item for item in results if item not in changed]
        message = _("Compatibility target change complete. %d manifests changed: %s. Use the Undo most recent compatibility target changes command if these changes need to be restored.") % (len(changed), self._limitedDetails(changed, lambda item: _("%s %s") % (item.name, item.status))) if changed else _("Compatibility target change complete. No manifests were changed.")
        if failed: message += _(" %d projects were not changed: %s.") % (len(failed), "; ".join(_("%s: %s") % (item.name, item.status.removeprefix("validation failed: ")) for item in failed))
        if saveError: message += _(" Warning: the manifest results or undo history could not be saved: %s. Keep the backup files and inspect the changed manifests before continuing.") % saveError
        self._showInformation(_("Compatibility target results"), message)
    @scriptHandler.script(description=_("Undo the most recent compatibility target changes"), category=SCRIPT_CATEGORY)
    def script_undoCompatibilityTargetChanges(self, gesture):
        state = engine.read_json(self._statePath, {}); entries = state.get("compatibilityUndo", []) if isinstance(state.get("compatibilityUndo"), list) else []
        entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") and entry.get("backup_path")]
        if not entries: ui.message(_("No compatibility target changes are available to undo")); return
        details = "; ".join(_("%s: NVDA %s back to %s; %s") % (entry.get("name", Path(entry["path"]).parent.name), entry.get("target_last_tested", "unknown"), entry.get("previous_last_tested", "unknown"), entry["path"]) for entry in entries)
        self._showConfirmation(_("Undo compatibility target changes"), _("Restore the manifests from the most recent compatibility target operation? Later edits will never be overwritten. Planned restores: %s.") % details, lambda: self._startUndoCompatibilityTargets(entries))
    @scriptHandler.script(description=_("Undo the most recent discovered compatibility updates"), category=SCRIPT_CATEGORY)
    def script_undoLatestCompatibilityUpdates(self, gesture):
        state = engine.read_json(self._statePath, {}); entries = state.get("updateUndo", []) if isinstance(state.get("updateUndo"), list) else []; entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") and entry.get("backup_path")]
        if not entries: ui.message(_("No compatibility updates are available to undo")); return
        details = "; ".join(_("%s: NVDA %s back to %s; %s") % (entry.get("name", Path(entry["path"]).parent.name), entry.get("target_last_tested", "unknown"), entry.get("previous_last_tested", "unknown"), entry["path"]) for entry in entries)
        self._showConfirmation(_("Undo compatibility updates"), _("Restore manifests from the most recent update operation? Later edits will never be overwritten. Planned restores: %s.") % details, lambda: self._startUndoCompatibilityTargets(entries, "updateUndo", True))
    def _startUndoCompatibilityTargets(self, entries, stateKey="compatibilityUndo", updateOperation=False):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        self._scanCancel.clear(); self._scanActive.set(); ui.message(_("Restoring manifests from the most recent compatibility update") if updateOperation else _("Restoring manifests from the most recent compatibility target operation")); self._startProgress()
        try:
            self._manifestThread = threading.Thread(target=self._undoCompatibilityTargets, args=(entries, stateKey, updateOperation), name="addonDeveloperCompatibilityUndo", daemon=True); self._manifestThread.start()
        except Exception: self._manifestThread = None; self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start compatibility undo"); ui.message(_("The compatibility changes could not be restored"))
    def _undoCompatibilityTargets(self, entries, stateKey="compatibilityUndo", updateOperation=False):
        results = []; saveError = ""
        try:
            for entry in entries:
                if self._scanCancel.is_set() or self._shutdown.is_set(): results.append(engine.ProjectResult(str(entry.get("project_id", "")), str(entry.get("name", "add-on")), str(entry.get("path", "")), "cancelled")); continue
                try: results.append(engine.undo_compatibility_target(Path(entry["path"]), Path(entry["backup_path"]), str(entry.get("target_last_tested", "")), str(entry.get("target_manifest_hash", "")), self._backupRoot))
                except Exception as error: results.append(engine.ProjectResult(str(entry.get("project_id", "")), str(entry.get("name", "add-on")), str(entry.get("path", "")), f"undo failed: {error}"))
            restored = {os.path.normcase(result.path) for result in results if result.status.startswith("restored last tested NVDA from ")}
            state = engine.read_json(self._statePath, {}); state["projects"] = engine.merge_project_records(state.get("projects", []), results); state[stateKey] = [entry for entry in entries if os.path.normcase(str(entry.get("path", ""))) not in restored]; engine.atomic_json_write(self._statePath, state)
            for result in results: log.info("Add-on Developer Updater compatibility undo: %s: %s", result.path, result.status)
        except Exception as error:
            saveError = str(error); log.exception("Compatibility undo results could not be saved")
        finally:
            self._scanActive.clear(); self._scanLock.release(); self._finishProgressThen(self._finishUndoCompatibilityTargets, results, updateOperation, saveError)
    def _finishUndoCompatibilityTargets(self, results, updateOperation=False, saveError=""):
        restored = [result for result in results if result.status.startswith("restored last tested NVDA from ")]; failed = [result for result in results if result not in restored]
        if restored:
            template = _("Compatibility update undo complete. %d manifests restored: %s.") if updateOperation else _("Compatibility undo complete. %d manifests restored: %s.")
            message = template % (len(restored), self._limitedDetails(restored, lambda item: _("%s %s") % (item.name, item.status)))
        else: message = _("Compatibility update undo complete. No manifests were restored.") if updateOperation else _("Compatibility undo complete. No manifests were restored.")
        if failed: message += _(" %d manifests were not restored: %s.") % (len(failed), "; ".join(_("%s: %s") % (item.name, item.status) for item in failed))
        if saveError: message += _(" Warning: restored manifests were not recorded in updater state: %s. Verify the manifests and backups manually.") % saveError
        self._showInformation(_("Compatibility update undo results") if updateOperation else _("Compatibility undo results"), message)
    @scriptHandler.script(description=_("Review previously discovered NVDA add-on compatibility updates"), category=SCRIPT_CATEGORY)
    def script_reviewAddonProjectUpdates(self, gesture):
        state = engine.read_json(self._statePath, {}); release, available = engine.load_manual_review(state)
        if release is not None and available:
            self._showReviewDialog(available, release); return
        available = []
        for item in engine.visible_project_records(state)[0]:
            if isinstance(item, dict) and str(item.get("status", "")).startswith("update available: "):
                try: available.append(engine.ProjectResult(**item))
                except TypeError: pass
        if not available: ui.message(_("No saved add-on compatibility updates are available")); return
        self._showReviewDialog(available, engine.Release(str(state.get("releaseTag", "")), str(state.get("manifestVersion", "")), bool(state.get("prerelease")), str(state.get("releaseUrl", "")), str(state.get("releaseSource", "saved")), str(state.get("releaseCheckedAt", "")), str(state.get("releaseBuildVersion", ""))))
    @scriptHandler.script(description=_("Cancel the running add-on developer operation"), category=SCRIPT_CATEGORY)
    def script_cancelAddonProjectScan(self, gesture):
        if not self._scanActive.is_set(): ui.message(_("No cancellable add-on developer operation is running")); return
        self._scanCancel.set()
        if self._workerProcess is not None:
            try: self._workerProcess.terminate()
            except OSError: pass
        ui.message(_("Cancellation requested. The operation stops at its next safe point; a GitHub push or upload already in progress is allowed to finish."))
    @scriptHandler.script(description=_("Submit updated add-ons to the NVDA Add-on Store"), gesture="kb:NVDA+alt+shift+s", category=SCRIPT_CATEGORY)
    def script_submitUpdatedAddons(self, gesture):
        self._beginApprovedProjectSelection("store")
    def _showStoreSubmissionDialog(self, ready):
        if self._storeDialog is not None:
            self._storeDialog.Raise(); self._storeDialog.checkList.SetFocus(); return
        dialog = StoreSubmissionDialog(gui.mainFrame, ready); self._storeDialog = dialog
        def close(_event=None):
            if self._storeDialog is dialog: self._storeDialog = None
            dialog.Destroy()
        def inspect(_event=None):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-ons were selected for store-submission review")); return
            close(); self._beginStorePreflight(selected)
        dialog.onUpdate = inspect; dialog.publishButton.Bind(wx.EVT_BUTTON, inspect); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _beginStorePreflight(self, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Checking the selected add-ons, release packages, and current NVDA Add-on Store requirements")); self._scanCancel.clear(); self._scanActive.set(); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._storePreflight, args=(ready,), name="addonDeveloperStorePreflight", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start store submission preflight"); ui.message(_("The store submission check could not be started"))
    def _beginApprovedProjectSelection(self, action):
        if action not in {"github", "store"}: return
        existing = self._publishDialog if action == "github" else self._storeDialog
        if existing is not None:
            existing.Raise(); existing.checkList.SetFocus(); return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        message = _("Loading tracked add-ons for GitHub publishing") if action == "github" else _("Loading tracked add-ons for the Add-on Store check")
        ui.message(message); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadApprovedProjectSelection, args=(action,), name="addonDeveloperProjectSelection", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start approved-project loading"); ui.message(_("Tracked add-ons could not be loaded"))
    def _loadApprovedProjectSelection(self, action):
        ready = []; ignoredCount = 0; olderCount = 0; errorMessage = ""
        try:
            state = engine.read_json(self._statePath, {}); records, ignored = engine.visible_project_records(state); records, older = engine.newest_local_project_records(records)
            approved = engine.approved_project_ids(state)
            for item in records:
                if not isinstance(item, dict) or item.get("project_id") not in approved: continue
                try:
                    result = engine.ProjectResult(**item); path = Path(result.path)
                    if not path.is_file(): continue
                    metadata = engine.values(path); result.public_name = metadata.get("summary") or result.name; ready.append(result)
                except (OSError, TypeError, ValueError):
                    log.exception("Could not load approved project metadata for %s", item.get("path"))
            ignoredCount = len(ignored); olderCount = len(older)
            if action == "github" and ignored:
                saved = set(state.get("ignoredProjects", [])) if isinstance(state.get("ignoredProjects"), list) else set()
                if ignored != saved: state["ignoredProjects"] = sorted(ignored); engine.atomic_json_write(self._statePath, state)
        except Exception as error:
            log.exception("Approved-project loading failed"); errorMessage = str(error)
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishApprovedProjectSelection, action, ready, ignoredCount, olderCount, errorMessage)
    def _finishApprovedProjectSelection(self, action, ready, ignoredCount, olderCount, errorMessage=""):
        if errorMessage:
            self._showInformation(_("Tracked add-ons could not be loaded"), _("Tracked add-ons could not be loaded: %s") % errorMessage); return
        if not ready:
            message = _("No tracked add-ons to publish. Press NVDA+Alt+Shift+U to search for add-ons, then check the ones you develop.") if action == "github" else _("No tracked add-ons to check for the Add-on Store. Press NVDA+Alt+Shift+U to search for add-ons, then check the ones you develop.")
            ui.message(message); return
        if action == "github":
            # One short sentence: the checklist opening next would cut off anything longer.
            hidden = ignoredCount + olderCount
            if hidden: ui.message(ngettext("%d duplicate or older copy is hidden; only the newest copy of each add-on is listed.", "%d duplicate or older copies are hidden; only the newest copy of each add-on is listed.", hidden) % hidden)
            self._showGitHubPublishDialog(ready)
        else: self._showStoreSubmissionDialog(ready)
    def _storePreflight(self, ready):
        callback = None; arguments = ()
        try:
            reports = publisher.preflight(ready, engine.values, self._githubProgress, self._githubCancelled)
            callback, arguments = self._offerStoreSubmission, (reports,)
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerStoreGitHubLogin, (ready,)
        except publisher.OperationCancelled:
            callback, arguments = self._showInformation, (_("Store-readiness check cancelled"), _("The store-readiness check was cancelled. No store pages were opened."))
        except Exception:
            # One malformed or inaccessible project must not hide other add-ons
            # that can be verified. Retry individually and retain exact failures.
            log.exception("Combined store submission preflight failed; retrying projects individually")
            reports = []; failures = []
            for project in ready:
                try: reports.extend(publisher.preflight([project], engine.values, self._githubProgress, self._githubCancelled))
                except publisher.GitHubCliRequired:
                    callback, arguments = self._offerGitHubCliInstall, (); break
                except publisher.AuthenticationRequired:
                    callback, arguments = self._offerStoreGitHubLogin, (ready,); break
                except publisher.OperationCancelled:
                    callback, arguments = self._showInformation, (_("Store-readiness check cancelled"), _("The store-readiness check was cancelled. No store pages were opened.")); break
                except Exception as projectError:
                    log.exception("Store submission inspection failed for %s", project.name)
                    failures.append(_("%s: inspection failed: %s") % (_projectDisplayName(project), projectError))
            else: callback, arguments = self._offerStoreSubmission, (reports, failures)
        finally:
            self._scanActive.clear(); self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerStoreGitHubLogin(self, ready):
        self._showConfirmation(_("Sign in to GitHub"), _("GitHub CLI is not signed in for NVDA. Sign in now to check the selected add-ons for store submission? A one-time code will be copied to the clipboard and GitHub will open in your browser."), lambda: self._startStoreGitHubLogin(ready), defaultYes=True, onNo=lambda: self._showInformation(_("Store-readiness check canceled"), _("The selected add-ons were not checked and no store pages were opened.")))
    def _startStoreGitHubLogin(self, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Starting GitHub sign-in. Complete authorization in the browser.")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._storeGitHubLogin, args=(ready,), name="addonDeveloperStoreGitHubLogin", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub sign-in"); ui.message(_("GitHub sign-in could not be started"))
    def _storeGitHubLogin(self, ready):
        callback = None; arguments = ()
        try:
            publisher.login(); callback, arguments = self._retryStorePreflight, (ready,)
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except Exception as error:
            log.exception("GitHub sign-in failed"); callback, arguments = self._showInformation, (_("GitHub sign-in failed"), _("GitHub sign-in failed: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _retryStorePreflight(self, ready):
        ui.message(_("GitHub sign-in completed. Retrying the store-readiness check.")); self._beginStorePreflight(ready)
    def _offerStoreSubmission(self, reports, inspectionFailures=()):
        self._rememberExistingReleases(reports); eligible = []; blocked = list(inspectionFailures)
        for report in reports:
            item = publisher.store_release_item(report)
            reasons = publisher.store_readiness_reasons(report)
            if reasons: blocked.append(_("%s: %s") % (publisher.public_name(report), ", ".join(reasons)))
            else: eligible.append((report, item))
        if not eligible:
            self._showGitHubResult(engine.readable_message(_("No add-ons are ready for the Add-on Store yet."), [_("What needs fixing:")] + blocked), _("Store submission not ready")); return
        names = ", ".join(publisher.public_name(report) for report, _item in eligible)
        settings = config.conf["addonDeveloperUpdater"]; fallbackLicense = settings["storeFallbackLicenseName"].strip()
        unknownLicense = [publisher.public_name(report) for report, _item in eligible if not report.license_name and not fallbackLicense]
        summary = ngettext("Open the Add-on Store submission form for %d add-on?", "Open the Add-on Store submission forms for %d add-ons?", len(eligible)) % len(eligible)
        details = [_("Ready: %s") % names, _("Each form opens in your browser with its fields filled in. Nothing is submitted until you review the form and submit it yourself.")]
        if unknownLicense: details += ["", _("No standard license file was found for: %s. Fill in the license fields on its form.") % ", ".join(unknownLicense)]
        if blocked: details += ["", _("Not ready yet:")] + blocked
        message = engine.readable_message(summary, details, _("Yes opens the forms. No opens nothing."))
        def submit():
            results = [engine.ProjectResult(report.project_id, report.name, report.manifest, "GitHub release verified") for report, _item in eligible]
            urls = [publisher.store_url(item, settings["storePublisher"], settings["storeFallbackLicenseName"], settings["storeFallbackLicenseUrl"]) for _report, item in eligible]
            ui.message(_("Opening prefilled store submission forms for: %s") % names); self._startSubmissionLauncher(results, urls)
        self._showConfirmation(_("Submit add-ons to the NVDA Add-on Store"), message, submit, onNo=lambda: self._showGitHubResult(_("Store submission was canceled. No store pages were opened.")))
    @scriptHandler.script(description=_("Publish updated add-ons to GitHub and optionally create releases"), gestures=("kb:NVDA+alt+g", "kb:NVDA+alt+shift+g"), category=SCRIPT_CATEGORY)
    def script_publishUpdatedAddonsToGitHub(self, gesture):
        self._beginApprovedProjectSelection("github")
    @scriptHandler.script(description=_("Copy a download or repository link for a released NVDA add-on"), gesture="kb:NVDA+alt+shift+f", category=SCRIPT_CATEGORY)
    def script_copyGitHubAddonRepositoryUrl(self, gesture):
        if self._repositoryDialog is not None:
            self._repositoryDialog.Raise(); self._repositoryDialog.repositoryList.SetFocus(); return
        self._beginGitHubRepositoryLookup()
    def _beginGitHubRepositoryLookup(self):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Loading released NVDA add-on files from the signed-in GitHub account")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadGitHubRepositories, name="addonDeveloperGitHubRepositories", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub repository lookup"); ui.message(_("The GitHub repository list could not be loaded"))
    def _loadGitHubRepositories(self):
        callback = None; arguments = ()
        try:
            owner, repositories = publisher.github_addon_repositories(self._githubProgress)
            if repositories: callback, arguments = self._showGitHubRepositoryDialog, (owner, repositories)
            else: callback, arguments = self._showInformation, (_("No released NVDA add-ons found"), _("No non-draft GitHub Releases with downloadable .nvda-addon files were found in the signed-in account."))
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerGitHubRepositoryLogin, ()
        except Exception as error:
            log.exception("GitHub repository lookup failed"); callback, arguments = self._showInformation, (_("GitHub repository lookup failed"), _("The NVDA add-on repository list could not be loaded: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showGitHubRepositoryDialog(self, owner, repositories):
        if self._repositoryDialog is not None: self._repositoryDialog.Raise(); return
        dialog = GitHubRepositoryUrlDialog(gui.mainFrame, owner, repositories); self._repositoryDialog = dialog
        def close(_event=None):
            if self._repositoryDialog is dialog: self._repositoryDialog = None
            dialog.Destroy()
        def copyUrl(_event=None):
            repository = dialog.selectedRepository()
            if repository is None: ui.message(_("No repository is selected")); return
            try: copied = api.copyToClip(repository.download_url)
            except Exception:
                log.exception("Could not copy GitHub add-on download URL"); copied = False
            if copied: ui.message(_("Copied direct download URL for %s, release %s, file %s") % (repository.name, repository.release_tag, repository.asset_name))
            else: ui.message(_("The add-on download URL could not be copied to the clipboard"))
        def copyRepositoryUrl(_event=None):
            repository = dialog.selectedRepository()
            if repository is None: ui.message(_("No repository is selected")); return
            try: copied = api.copyToClip(repository.url)
            except Exception:
                log.exception("Could not copy GitHub repository URL"); copied = False
            if copied: ui.message(_("Copied repository link for %s") % repository.full_name)
            else: ui.message(_("The repository link could not be copied to the clipboard"))
        dialog.onCopy = copyUrl; dialog.onCopyRepository = copyRepositoryUrl; dialog.copyButton.Bind(wx.EVT_BUTTON, copyUrl); dialog.copyRepositoryButton.Bind(wx.EVT_BUTTON, copyRepositoryUrl); dialog.repositoryList.Bind(wx.EVT_LISTBOX_DCLICK, copyUrl); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.repositoryList.SetFocus)
    def _offerGitHubRepositoryLogin(self):
        self._showConfirmation(_("Sign in to GitHub"), _("GitHub CLI is not signed in for NVDA. Sign in now to load your released NVDA add-on files? A one-time code will be copied to the clipboard and GitHub will open in your browser."), self._startGitHubRepositoryLogin, defaultYes=True, onNo=lambda: self._showInformation(_("Add-on download lookup canceled"), _("No add-on download URL was copied.")))
    def _startGitHubRepositoryLogin(self):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Starting GitHub sign-in. Complete authorization in the browser.")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubRepositoryLogin, name="addonDeveloperGitHubRepositoryLogin", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub sign-in"); ui.message(_("GitHub sign-in could not be started"))
    def _githubRepositoryLogin(self):
        callback = None; arguments = ()
        try:
            publisher.login(); callback, arguments = self._beginGitHubRepositoryLookup, ()
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except Exception as error:
            log.exception("GitHub sign-in failed"); callback, arguments = self._showInformation, (_("GitHub sign-in failed"), _("GitHub sign-in failed: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    @scriptHandler.script(description=_("Open the Issues page for an NVDA add-on repository"), gesture="kb:NVDA+alt+shift+i", category=SCRIPT_CATEGORY)
    def script_openGitHubAddonIssues(self, gesture): self._beginGitHubProjectAction("issues")
    @scriptHandler.script(description=_("Edit a GitHub issue template for an NVDA add-on repository"), gesture="kb:NVDA+alt+shift+t", category=SCRIPT_CATEGORY)
    def script_editGitHubAddonIssueTemplate(self, gesture): self._beginGitHubProjectAction("templates")
    def _beginGitHubProjectAction(self, action):
        if action not in {"issues", "templates"}: return
        if self._githubSelectionDialog is not None:
            if self._githubSelectionAction == action:
                self._githubSelectionDialog.Raise(); self._githubSelectionDialog.itemList.SetFocus(); return
            dialog = self._githubSelectionDialog; self._githubSelectionDialog = None; self._githubSelectionAction = None; dialog.Destroy()
        if self._templateEditorDialog is not None:
            self._templateEditorDialog.Raise(); self._templateEditorDialog.initialFocus().SetFocus(); ui.message(_("Close the open issue-template editor before starting another repository action")); return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        message = _("Loading add-on repositories and Issues settings from GitHub") if action == "issues" else _("Loading add-on repositories and issue templates from GitHub")
        ui.message(message); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadGitHubProjects, args=(action,), name="addonDeveloperGitHubProjects", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub project lookup"); ui.message(_("The GitHub add-on repository list could not be loaded"))
    def _loadGitHubProjects(self, action):
        callback = None; arguments = ()
        try:
            owner, repositories = publisher.github_addon_projects(self._githubProgress)
            if repositories: callback, arguments = self._showGitHubProjectActionDialog, (owner, repositories, action)
            else: callback, arguments = self._showInformation, (_("No NVDA add-on repositories found"), _("No repositories containing recognized NVDA add-on project files were found in the signed-in GitHub account."))
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerGitHubProjectLogin, (action,)
        except Exception as error:
            log.exception("GitHub project lookup failed"); callback, arguments = self._showInformation, (_("GitHub repository lookup failed"), _("The NVDA add-on repository list could not be loaded: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showGitHubProjectActionDialog(self, owner, repositories, action):
        if self._githubSelectionDialog is not None: self._githubSelectionDialog.Raise(); return
        choices = []
        for repository in repositories:
            status = _("private") if repository.private else _("public")
            if repository.archived: status += _(", archived")
            if action == "issues" and not repository.issues_enabled: status += _(", Issues disabled")
            choices.append(_("%s; %s; default branch %s") % (repository.full_name, status, repository.default_branch or _("none")))
        title = _("Open an add-on repository's Issues page") if action == "issues" else _("Choose a repository whose issue template should be edited")
        instruction = _("Select a repository owned by %s. Press Enter to continue or Escape to close.") % owner
        actionLabel = _("&Open Issues") if action == "issues" else _("&Choose repository")
        dialog = GitHubSingleSelectionDialog(gui.mainFrame, title, instruction, repositories, choices, actionLabel); self._githubSelectionDialog = dialog; self._githubSelectionAction = action
        def close(_event=None):
            if self._githubSelectionDialog is dialog: self._githubSelectionDialog = None; self._githubSelectionAction = None
            dialog.Destroy()
        def act(_event=None):
            repository = dialog.selectedItem()
            if repository is None: ui.message(_("No repository is selected")); return
            if action == "issues":
                if not repository.issues_enabled: ui.message(_("GitHub Issues are disabled for %s") % repository.full_name); return
                close(); self._openGitHubUrl(repository.url.rstrip("/") + "/issues", repository.full_name)
            else:
                if repository.archived: ui.message(_("%s is archived and its issue templates cannot be changed") % repository.full_name); return
                close(); self._beginIssueTemplateLookup(repository)
        dialog.onAction = act; dialog.actionButton.Bind(wx.EVT_BUTTON, act); dialog.itemList.Bind(wx.EVT_LISTBOX_DCLICK, act); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.itemList.SetFocus)
    def _openGitHubUrl(self, url, repositoryName):
        def launch():
            try:
                if self._shutdown.wait(0.2): return
                os.startfile(url)
            except OSError:
                log.exception("Could not open GitHub Issues page")
                if not self._shutdown.is_set(): wx.CallAfter(ui.message, _("The Issues page for %s could not be opened") % repositoryName)
        ui.message(_("Opening the Issues page for %s") % repositoryName)
        try: threading.Thread(target=launch, name="addonDeveloperOpenGitHubIssues", daemon=True).start()
        except Exception:
            log.exception("Could not start GitHub Issues page launcher"); ui.message(_("The GitHub Issues page could not be opened"))
    def _beginIssueTemplateLookup(self, repository):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Loading issue templates for %s") % repository.full_name); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadIssueTemplates, args=(repository,), name="addonDeveloperIssueTemplates", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start issue-template lookup"); ui.message(_("The issue-template list could not be loaded"))
    def _loadIssueTemplates(self, repository):
        callback = None; arguments = ()
        try:
            templates = publisher.github_issue_templates(repository)
            if templates: callback, arguments = self._showIssueTemplateDialog, (repository, templates)
            else: callback, arguments = self._showInformation, (_("No issue templates found"), _("%s has no editable Markdown or YAML files in .github/ISSUE_TEMPLATE on its default branch.") % repository.full_name)
        except Exception as error:
            log.exception("Issue-template lookup failed"); callback, arguments = self._showInformation, (_("Issue-template lookup failed"), _("Issue templates for %s could not be loaded: %s") % (repository.full_name, error))
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showIssueTemplateDialog(self, repository, templates):
        choices = [_('%s; path %s') % (template.name, template.path) for template in templates]
        dialog = GitHubSingleSelectionDialog(gui.mainFrame, _("Choose a GitHub issue template"), _("Select an issue template from %s and press Enter to edit it.") % repository.full_name, templates, choices, _("&Edit template")); self._githubSelectionDialog = dialog; self._githubSelectionAction = "template-file"
        def close(_event=None):
            if self._githubSelectionDialog is dialog: self._githubSelectionDialog = None; self._githubSelectionAction = None
            dialog.Destroy()
        def edit(_event=None):
            template = dialog.selectedItem()
            if template is None: ui.message(_("No issue template is selected")); return
            close(); self._beginIssueTemplateLoad(repository, template)
        dialog.onAction = edit; dialog.actionButton.Bind(wx.EVT_BUTTON, edit); dialog.itemList.Bind(wx.EVT_LISTBOX_DCLICK, edit); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.itemList.SetFocus)
    def _beginIssueTemplateLoad(self, repository, template):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Loading %s from GitHub") % template.name); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._loadIssueTemplate, args=(repository, template), name="addonDeveloperIssueTemplateLoad", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start issue-template load"); ui.message(_("The issue template could not be loaded"))
    def _loadIssueTemplate(self, repository, template):
        callback = None; arguments = ()
        try:
            loaded = publisher.load_github_issue_template(repository, template)
            try: document = publisher.parse_issue_template_document(loaded)
            except (TypeError, ValueError): document = None
            callback, arguments = self._showIssueTemplateEditor, (repository, loaded, document)
        except Exception as error:
            log.exception("Issue-template load failed"); callback, arguments = self._showInformation, (_("Issue-template load failed"), _("%s could not be loaded: %s") % (template.name, error))
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showIssueTemplateEditor(self, repository, template, document):
        if self._templateEditorDialog is not None: self._templateEditorDialog.Raise(); return
        dialog = IssueTemplateEditorDialog(gui.mainFrame, repository, template, document); self._templateEditorDialog = dialog
        def close(_event=None):
            if self._templateEditorDialog is dialog: self._templateEditorDialog = None
            dialog.Destroy()
        def save(_event=None):
            content = dialog.getContent()
            if content == template.content: ui.message(_("The issue template has not changed")); return
            if not content: ui.message(_("The issue template cannot be empty")); return
            self._beginIssueTemplateSaveValidation(repository, template, content, dialog)
        def delete(_event=None):
            message = _("Delete %s from %s on its %s branch? This creates a deletion commit and cannot be undone from this dialog. The file will be deleted only if it has not changed on GitHub since it was loaded.") % (template.name, repository.full_name, repository.default_branch)
            self._showConfirmation(_("Delete GitHub issue template"), message, lambda: self._startIssueTemplateDelete(repository, template, dialog))
        dialog.onSave = save; dialog.onDelete = delete; dialog.saveButton.Bind(wx.EVT_BUTTON, save); dialog.deleteButton.Bind(wx.EVT_BUTTON, delete); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.initialFocus().SetFocus)
    def _beginIssueTemplateSaveValidation(self, repository, template, content, dialog):
        if self._templateEditorDialog is not dialog: return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        dialog.Disable(); ui.message(_("Checking %s before saving") % template.name); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._validateIssueTemplateForSave, args=(repository, template, content, dialog), name="addonDeveloperIssueTemplateValidation", daemon=True).start()
        except Exception:
            dialog.Enable(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start issue-template validation"); ui.message(_("The issue template could not be checked"))
    def _validateIssueTemplateForSave(self, repository, template, content, dialog):
        valid = False; errorMessage = ""
        try:
            publisher.validate_issue_template_document(template, publisher.parse_issue_template_document(template, content)); valid = True
        except (TypeError, ValueError) as error: errorMessage = str(error)
        except Exception as error:
            log.exception("Issue-template validation failed"); errorMessage = str(error)
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishIssueTemplateValidation, repository, template, content, dialog, valid, errorMessage)
    def _finishIssueTemplateValidation(self, repository, template, content, dialog, valid, errorMessage):
        if self._templateEditorDialog is not dialog: return
        dialog.Enable()
        if not valid:
            dialog.Raise(); dialog.initialFocus().SetFocus(); self._showInformation(_("Issue template is not valid"), _("The issue template was not saved: %s") % errorMessage); return
        message = _("Commit these changes to %s on its %s branch? The existing file will be updated only if it has not changed on GitHub since it was loaded.") % (repository.full_name, repository.default_branch)
        self._showConfirmation(_("Save issue template to GitHub"), message, lambda: self._startIssueTemplateSave(repository, template, content, dialog))
    def _startIssueTemplateSave(self, repository, template, content, dialog):
        if self._templateEditorDialog is not dialog: return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        dialog.Disable(); ui.message(_("Saving %s to GitHub") % template.name); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._saveIssueTemplate, args=(repository, template, content, dialog), name="addonDeveloperIssueTemplateSave", daemon=True).start()
        except Exception:
            dialog.Enable(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start issue-template save"); ui.message(_("The issue template could not be saved"))
    def _saveIssueTemplate(self, repository, template, content, dialog):
        succeeded = False; message = ""
        try:
            publisher.update_github_issue_template(repository, template, content); succeeded = True; message = _("Saved %s to %s on branch %s.") % (template.name, repository.full_name, repository.default_branch)
        except Exception as error:
            log.exception("Issue-template save failed"); message = _("The issue template was not saved: %s") % error
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishIssueTemplateSave, dialog, succeeded, message)
    def _finishIssueTemplateSave(self, dialog, succeeded, message):
        if self._templateEditorDialog is dialog:
            if succeeded: self._templateEditorDialog = None; dialog.Destroy()
            else: dialog.Enable(); dialog.Raise(); dialog.initialFocus().SetFocus()
        self._showInformation(_("Issue template saved") if succeeded else _("Issue-template save failed"), message)
    def _startIssueTemplateDelete(self, repository, template, dialog):
        if self._templateEditorDialog is not dialog: return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        dialog.Disable(); ui.message(_("Deleting %s from GitHub") % template.name); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._deleteIssueTemplate, args=(repository, template, dialog), name="addonDeveloperIssueTemplateDelete", daemon=True).start()
        except Exception:
            dialog.Enable(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start issue-template deletion"); ui.message(_("The issue template could not be deleted"))
    def _deleteIssueTemplate(self, repository, template, dialog):
        succeeded = False; message = ""
        try:
            publisher.delete_github_issue_template(repository, template); succeeded = True; message = _("Deleted %s from %s on branch %s.") % (template.name, repository.full_name, repository.default_branch)
        except Exception as error:
            log.exception("Issue-template deletion failed"); message = _("The issue template was not deleted: %s") % error
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishIssueTemplateDelete, dialog, succeeded, message)
    def _finishIssueTemplateDelete(self, dialog, succeeded, message):
        if self._templateEditorDialog is dialog:
            if succeeded: self._templateEditorDialog = None; dialog.Destroy()
            else: dialog.Enable(); dialog.Raise(); dialog.initialFocus().SetFocus()
        self._showInformation(_("Issue template deleted") if succeeded else _("Issue-template deletion failed"), message)
    def _offerGitHubProjectLogin(self, action):
        self._showConfirmation(_("Sign in to GitHub"), _("GitHub CLI is not signed in for NVDA. Sign in now to access add-on repository Issues and templates? A one-time code will be copied to the clipboard and GitHub will open in your browser."), lambda: self._startGitHubProjectLogin(action), defaultYes=True, onNo=lambda: self._showInformation(_("GitHub repository action canceled"), _("No GitHub page was opened and no issue template was changed.")))
    def _startGitHubProjectLogin(self, action):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Starting GitHub sign-in. Complete authorization in the browser.")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubProjectLogin, args=(action,), name="addonDeveloperGitHubProjectLogin", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub sign-in"); ui.message(_("GitHub sign-in could not be started"))
    def _githubProjectLogin(self, action):
        callback = None; arguments = ()
        try: publisher.login(); callback, arguments = self._beginGitHubProjectAction, (action,)
        except publisher.GitHubCliRequired: callback, arguments = self._offerGitHubCliInstall, ()
        except Exception as error:
            log.exception("GitHub sign-in failed"); callback, arguments = self._showInformation, (_("GitHub sign-in failed"), _("GitHub sign-in failed: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showGitHubPublishDialog(self, ready):
        if self._publishDialog is not None:
            self._publishDialog.Raise(); self._publishDialog.checkList.SetFocus(); return
        dialog = GitHubPublishDialog(gui.mainFrame, ready); self._publishDialog = dialog
        def close(_event=None):
            if self._publishDialog is dialog: self._publishDialog = None
            dialog.Destroy()
        def publish(_event=None):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-on projects were selected for GitHub publishing")); return
            close(); self._beginGitHubPreflight(selected)
        dialog.onUpdate = publish; dialog.publishButton.Bind(wx.EVT_BUTTON, publish); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _beginGitHubPreflight(self, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Checking GitHub authentication and add-on repository safety")); self._scanCancel.clear(); self._scanActive.set(); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubPreflight, args=(ready,), name="addonDeveloperGitHubPreflight", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub preflight"); ui.message(_("The GitHub preflight could not be started"))
    def _monitorLoop(self):
        while not self._shutdown.is_set():
            try:
                settings = config.conf["addonDeveloperUpdater"]
                state = engine.read_json(self._statePath, {})
                wait_seconds = self._nextAutomaticCheckDelay(state, settings) if settings["automaticChecks"] else 900
            except Exception:
                log.exception("Add-on Developer Updater monitor failed")
                wait_seconds = 900
            if self._wakeMonitor.wait(wait_seconds): self._wakeMonitor.clear(); continue
            if self._shutdown.is_set(): break
            try:
                if config.conf["addonDeveloperUpdater"]["automaticChecks"]:
                    self._scanCancel.clear(); self._runCheck(manual=False, full_system=False)
            except Exception:
                log.exception("Add-on Developer Updater monitor failed")
    @staticmethod
    def _nextAutomaticCheckDelay(state, settings):
        return engine.automatic_check_delay(state, int(settings["intervalMinutes"]))
    def _startCheck(self, manual=False, full_system=False, selectedRelease=None):
        if manual and selectedRelease is None:
            if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
            ui.message(_("Loading available NVDA builds")); self._startProgress(keepFocus=True)
            try: threading.Thread(target=self._loadManualScanTargets, args=(full_system,), name="addonDeveloperScanTargets", daemon=True).start()
            except Exception:
                self._stopProgress(); self._scanLock.release(); log.exception("Could not start NVDA scan-target lookup"); ui.message(_("Available NVDA builds could not be loaded"))
            return
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer scan is already running")); return
        self._scanCancel.clear(); self._scanActive.set(); ui.message(_("Checking for NVDA releases and add-on projects"))
        try: threading.Thread(target=self._runCheck, kwargs={"manual": manual, "full_system": full_system, "lock_acquired": True, "selectedRelease": selectedRelease}, name="addonDeveloperManualScan", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._scanLock.release(); log.exception("Could not start add-on developer scan"); ui.message(_("The add-on developer scan could not be started"))
    def _loadManualScanTargets(self, full_system):
        callback = None; arguments = ()
        try:
            includePrereleases = bool(config.conf["addonDeveloperUpdater"]["includePrereleases"])
            releases = engine.available_releases(includePrereleases, self._releaseCachePath)
            if not releases: raise RuntimeError("no selectable NVDA builds were returned")
            if includePrereleases or len(releases) > 1:
                preferred = str(engine.read_json(self._statePath, {}).get("manualReleaseTargetTag", ""))
                callback, arguments = self._showScanTargetDialog, (releases, preferred, full_system)
            else:
                callback, arguments = self._startCheck, (True, full_system, releases[0])
        except Exception as error:
            log.exception("NVDA scan-target lookup failed"); callback, arguments = self._showInformation, (_("Available NVDA builds could not be loaded"), _("Available NVDA builds could not be loaded: %s") % error)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _showScanTargetDialog(self, releases, preferredTag, full_system):
        if self._scanTargetDialog is not None: self._scanTargetDialog.Raise(); self._scanTargetDialog.targetList.SetFocus(); return
        dialog = ScanTargetDialog(gui.mainFrame, releases, preferredTag); self._scanTargetDialog = dialog
        def close(_event=None):
            if self._scanTargetDialog is dialog: self._scanTargetDialog = None
            dialog.Destroy()
        def start(_event=None):
            release = dialog.selectedRelease()
            if release is None: ui.message(_("No NVDA build was selected")); return
            close(); self._startCheck(True, full_system, release)
        dialog.onStart = start; dialog.startButton.Bind(wx.EVT_BUTTON, start); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.targetList.SetFocus)
    def _rootStrings(self):
        return engine.development_roots(config.conf["addonDeveloperUpdater"]["scanRoots"])
    @staticmethod
    def _limitedDetails(items, formatter, limit=5):
        details = [formatter(item) for item in items[:limit]]
        if len(items) > limit: details.append(_("and %d more") % (len(items) - limit))
        return "; ".join(details)
    @classmethod
    def _completionMessage(cls, results, heading=None):
        if not results: return _("Scan complete. No add-on development projects were found.") if heading is None else heading
        current = [result for result in results if result.status == "current"]
        updated = [result for result in results if result.status.startswith("updated to ")]
        available = [result for result in results if result.status.startswith("update available: ")]
        awaiting = [result for result in results if "awaiting" in result.status]
        failed = [result for result in results if "failed" in result.status]
        deferred = [result for result in results if "deferred" in result.status]
        # The first line carries the counts, because NVDA speaks it on its own
        # when the window opens and a braille display shows it first.
        counts = [text for items, text in (
            (updated, ngettext("%d manifest updated", "%d manifests updated", len(updated)) % len(updated)),
            (available, ngettext("%d update available", "%d updates available", len(available)) % len(available)),
            (awaiting, ngettext("%d add-on found but not tracked yet", "%d add-ons found but not tracked yet", len(awaiting)) % len(awaiting)),
            (failed, ngettext("%d project could not be checked", "%d projects could not be checked", len(failed)) % len(failed)),
            (deferred, ngettext("%d update deferred", "%d updates deferred", len(deferred)) % len(deferred)),
            (current, ngettext("%d project current", "%d projects current", len(current)) % len(current)),
        ) if items]
        heading = (_("Scan complete.") if heading is None else heading).rstrip(".")
        parts = []
        if updated:
            details = cls._limitedDetails(updated, lambda result: _("%s to NVDA %s") % (result.summary or result.name, result.status.removeprefix("updated to ")))
            parts.append(_("1 manifest updated: %s.") % details if len(updated) == 1 else _("%d manifests updated: %s.") % (len(updated), details))
        if available:
            details = cls._limitedDetails(available, lambda result: _("%s, NVDA %s") % (result.summary or result.name, result.status.removeprefix("update available: ")))
            parts.append(_("1 update is available: %s. The result was saved and nothing was changed. Use the review command when you are ready.") % details if len(available) == 1 else _("%d updates are available: %s. The results were saved and nothing was changed. Use the review command when you are ready.") % (len(available), details))
        if awaiting:
            details = cls._limitedDetails(awaiting, lambda result: result.summary or result.name)
            parts.append(_("Not tracked yet: %s.") % details)
        if failed:
            details = cls._limitedDetails(failed, lambda result: result.summary or result.name)
            parts.append(_("1 project could not be processed: %s. See the NVDA log for details.") % details if len(failed) == 1 else _("%d projects could not be processed: %s. See the NVDA log for details.") % (len(failed), details))
        if deferred:
            details = cls._limitedDetails(deferred, lambda result: _("%s: %s") % (result.summary or result.name, result.status))
            parts.append(_("The update was deferred: %s.") % details)
        summary = _("%s: %s.") % (heading, ", ".join(counts))
        return summary + ("\n" + engine.readable_text(" ".join(parts)) if parts else "")
    @staticmethod
    def _addProjectContext(result, path):
        try:
            metadata = engine.values(path); result.summary = metadata.get("summary", ""); result.minimum_version = metadata.get("minimumnvdaversion", ""); result.addon_version = metadata.get("version", ""); result.branch, result.manifest_changed = engine.git_manifest_state(path)
        except (OSError, UnicodeError, ValueError):
            pass
        return result
    def _showProjectApprovalDialog(self, awaiting, release):
        if self._approvalDialog is not None: self._approvalDialog.Raise(); self._approvalDialog.checkList.SetFocus(); return
        # Versions of each add-on that are already tracked, so the list can say so.
        state = engine.read_json(self._statePath, {}); tracked = engine.saved_approved_project_ids(state); trackedVersions = {}
        for item in state.get("projects", []) if isinstance(state.get("projects"), list) else []:
            if isinstance(item, dict) and item.get("project_id") in tracked:
                trackedVersions.setdefault(str(item.get("summary") or item.get("name") or "").casefold(), []).append(str(item.get("addon_version") or "unknown"))
        dialog = ProjectApprovalDialog(gui.mainFrame, awaiting, trackedVersions); self._approvalDialog = dialog
        def close(_event=None):
            if self._approvalDialog is dialog: self._approvalDialog = None
            dialog.Destroy()
        def approve(_event=None):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-ons are checked. Press Space on each add-on you develop.")); return
            summary = ngettext("Track %d add-on?", "Track %d add-ons?", len(selected)) % len(selected)
            details = [_("%s %s, in %s") % (item.summary or item.name, item.addon_version, item.path) for item in selected]
            details += ["", _("Tracked add-ons are checked for new NVDA versions now and after each scan, and can be published to GitHub and submitted to the Add-on Store."), _("Nothing in their files changes until you confirm an update or publication separately.")]
            message = engine.readable_message(summary, details, _("Yes starts tracking them. No returns to the list."))
            self._showConfirmation(_("Track add-ons"), message, lambda: (close(), self._startApproveProjects(selected, release)))
        dialog.onUpdate = approve; dialog.updateButton.Bind(wx.EVT_BUTTON, approve); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _startApproveProjects(self, selected, release):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(ngettext("Checking %d add-on before tracking it", "Checking %d add-ons before tracking them", len(selected)) % len(selected)); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._approveProjects, args=(selected, release, self._progressStop), name="addonDeveloperProjectApproval", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start project approval"); ui.message(_("Tracking could not be started"))
    def _approveProjects(self, selected, release, progressEvent):
        results = []; operationError = ""
        try:
            state = engine.read_json(self._statePath, {}); approved = engine.approved_project_ids(state)
            available = engine.available_releases(release.prerelease or bool(config.conf["addonDeveloperUpdater"]["includePrereleases"]), self._releaseCachePath)
            refreshed = engine.matching_release(release, available)
            if refreshed is None: raise RuntimeError(f"the selected NVDA build {release.tag} is no longer an available scan target; run the scan again")
            release = refreshed
            for index, item in enumerate(selected, 1):
                path = Path(item.path)
                wx.CallAfter(self._setProgressStep, progressEvent, _("Validating project %d of %d: %s") % (index, len(selected), item.name), index, len(selected))
                try: result = self._addProjectContext(engine.update(path, release, self._backupRoot, False), path)
                except Exception as error: result = engine.ProjectResult(item.project_id, item.name, item.path, f"validation failed: {error}")
                if "failed" in result.status: result.status = "awaiting manual approval; " + result.status
                else: approved.add(result.project_id)
                results.append(result)
            state["approvedProjects"] = sorted(approved); state["projects"] = engine.merge_project_records(state.get("projects", []), results); engine.save_manual_review(state, release, results, merge=True); engine.atomic_json_write(self._statePath, state)
        except Exception as error:
            operationError = str(error); log.exception("Project approval failed")
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishProjectApproval, results, release, operationError)
    def _finishProjectApproval(self, results, release, operationError=""):
        if operationError:
            message = engine.readable_message(_("The add-ons could not be tracked."), [_("Reason: %s") % operationError, _("No add-on was added to the tracked list.")]); self._showInformation(_("Tracking failed"), message); return
        approved = [item for item in results if "awaiting" not in item.status]; failed = [item for item in results if item not in approved]; available = [item for item in approved if item.status.startswith("update available: ")]
        summary = ngettext("Now tracking %d add-on.", "Now tracking %d add-ons.", len(approved)) % len(approved) if approved else _("No add-ons were tracked.")
        details = [_("Tracked: %s") % ", ".join(f"{item.summary or item.name} {item.addon_version}".strip() for item in approved)] if approved else []
        if failed: details += ["", ngettext("%d add-on could not be tracked because its manifest.ini has a problem:", "%d add-ons could not be tracked because their manifest.ini files have problems:", len(failed)) % len(failed)] + [_("%s: %s") % (item.summary or item.name, item.status.removeprefix("awaiting manual approval; ")) for item in failed] + [_("Fix the manifest, then press NVDA+Alt+Shift+U to check again.")]
        message = engine.readable_message(summary, details)
        if available:
            # Only a short summary is spoken: the review window opening next would cut off anything longer.
            ui.message(ngettext("Now tracking %d add-ons; %d has an NVDA update to review.", "Now tracking %d add-ons; %d have NVDA updates to review.", len(available)) % (len(approved), len(available)))
            self._showReviewDialog(available, release)
        else: self._showInformation(_("Tracked add-ons"), message)
    def _showReviewDialog(self, available, release):
        if self._reviewDialog is not None:
            self._reviewDialog.Raise(); self._reviewDialog.checkList.SetFocus(); return
        dialog = UpdateReviewDialog(gui.mainFrame, available, release); self._reviewDialog = dialog
        def closeDialog(_event=None):
            if self._reviewDialog is dialog: self._reviewDialog = None
            dialog.Destroy()
        def updateSelected(_event):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-on manifests were selected")); return
            details = "; ".join(_("%s: %s; branch %s; %s") % (item.name, item.status.removeprefix("update available: "), item.branch or _("unknown"), item.path) for item in selected)
            dirty = [item.name for item in selected if item.manifest_changed]
            build = release.tag + (f" ({release.build_version})" if release.build_version and release.build_version != release.tag else "")
            message = _("Update only lastTestedNVDAVersion for %d selected manifests using NVDA build %s and manifest compatibility target %s? This exact selected build will be rechecked before writing. Planned changes: %s.") % (len(selected), build, release.manifest_version, details)
            if dirty: message += _(" Warning: these manifests have uncommitted changes: %s. Current contents will be backed up before editing.") % "; ".join(dirty)
            self._showConfirmation(_("Confirm add-on compatibility updates"), message, lambda: (closeDialog(), self._startApply(selected, release)))
        dialog.onUpdate = updateSelected
        dialog.updateButton.Bind(wx.EVT_BUTTON, updateSelected); dialog.closeButton.Bind(wx.EVT_BUTTON, closeDialog); dialog.Bind(wx.EVT_CLOSE, closeDialog)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _startApply(self, selected, release):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        self._scanCancel.clear(); self._scanActive.set(); ui.message(_("Updating %d selected add-on manifests") % len(selected)); self._startProgress()
        try:
            self._manifestThread = threading.Thread(target=self._applySelected, args=(selected, release), name="addonDeveloperApplySelected", daemon=True); self._manifestThread.start()
        except Exception:
            self._manifestThread = None; self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start selected manifest updates"); ui.message(_("The selected manifest updates could not be started"))
    def _applySelected(self, selected, release):
        cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set(); results = []
        try:
            available = engine.available_releases(release.prerelease or bool(config.conf["addonDeveloperUpdater"]["includePrereleases"]), self._releaseCachePath)
            refreshed = engine.matching_release(release, available)
            if refreshed is None:
                results = [engine.ProjectResult(item.project_id, item.name, item.path, _("update deferred because the selected NVDA build %s is no longer an available target; run the update check again") % release.tag) for item in selected]
                state = engine.read_json(self._statePath, {}); state.pop("manualUpdateReview", None); engine.atomic_json_write(self._statePath, state)
                return
            release = refreshed
            for selectedResult in selected:
                if cancelled():
                    results.append(engine.ProjectResult(selectedResult.project_id, selectedResult.name, selectedResult.path, "cancelled")); continue
                path = Path(selectedResult.path)
                try: results.append(self._addProjectContext(engine.update(path, release, self._backupRoot, True, cancelled), path))
                except Exception as error:
                    log.exception("Could not update selected add-on manifest %s", path)
                    results.append(engine.ProjectResult(selectedResult.project_id, selectedResult.name, str(path), f"validation failed: {error}"))
            state = engine.read_json(self._statePath, {}); projects = engine.merge_project_records(state.get("projects", []), results)
            updated = [result for result in results if result.status.startswith("updated to ")]
            submissionProjects = engine.merge_project_records(state.get("submissionProjects", []), updated)
            channels = state.get("submissionChannels", {}) if isinstance(state.get("submissionChannels"), dict) else {}
            for result in updated: channels[result.project_id] = engine.submission_channel(release)
            state["projects"] = projects; state["submissionProjects"] = submissionProjects; state["submissionChannels"] = channels; state["lastScanAt"] = engine.utc_now()
            if updated: state["updateUndo"] = [{"project_id": result.project_id, "name": result.name, "path": result.path, "backup_path": result.backup_path, "previous_last_tested": result.previous_last_tested, "target_last_tested": result.target_last_tested, "target_manifest_hash": result.target_manifest_hash} for result in updated]
            completed = {result.project_id for result in results if result.status == "current" or result.status.startswith("updated to ")}
            engine.finish_manual_review_items(state, completed)
            engine.atomic_json_write(self._statePath, state)
            for result in results: log.info("Add-on Developer Updater selected update: %s: %s", result.path, result.status)
        except Exception as error:
            log.exception("Selected manifest update operation failed")
            results = [engine.ProjectResult(item.project_id, item.name, item.path, _("update operation failed while saving complete results; inspect the manifest and its backup: %s") % error) for item in selected]
        finally:
            self._scanActive.clear(); self._scanLock.release(); self._finishProgressThen(self._finishApply, results)
    def _finishApply(self, results):
        message = self._completionMessage(results, _("Compatibility update complete.")); self._showInformation(_("Compatibility update results"), message)
    def _showConfirmation(self, title, message, onYes, defaultYes=False, onNo=None):
        """Ask a question whose first line NVDA speaks; arrows and braille review the details.

        Escape always means No.  Enter in the message chooses the highlighted
        default button, which is No unless the action is harmless.
        """
        if self._confirmDialog is not None:
            ui.message(_("Finish the current confirmation before starting another add-on developer action")); self._confirmDialog.Raise(); self._confirmDialog.message.SetFocus(); return
        dialog = ConfirmationDialog(gui.mainFrame, title, engine.readable_text(message), defaultYes=defaultYes); self._confirmDialog = dialog
        def close(_event=None):
            if self._confirmDialog is dialog: self._confirmDialog = None
            dialog.Destroy()
        def yes(_event=None): close(); onYes()
        def no(_event=None):
            close()
            if onNo is not None: onNo()
        def onKey(event):
            key = event.GetKeyCode()
            if key == wx.WXK_ESCAPE: no(); return
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and dialog.message.HasFocus():
                (yes if defaultYes else no)(); return
            event.Skip()
        dialog.yesButton.Bind(wx.EVT_BUTTON, yes); dialog.noButton.Bind(wx.EVT_BUTTON, no); dialog.Bind(wx.EVT_CLOSE, close); dialog.Bind(wx.EVT_CHAR_HOOK, onKey)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.message.SetFocus)
    def _showInformation(self, title, message):
        """Show a result once: NVDA speaks its first line, and arrows or braille review the rest.

        Callers should not also speak the message; that made NVDA say it twice.
        """
        message = engine.readable_text(message)
        if self._informationDialog is not None:
            dialog = self._informationDialog; combined = engine.information_window_text(message, dialog.GetTitle(), dialog.message.GetValue(), _("Earlier result"))
            hadFocus = dialog.message.HasFocus()
            dialog.SetTitle(title); dialog.message.SetValue(combined); dialog.message.SetInsertionPoint(0); dialog.Raise()
            # A field that already has focus is not announced again, so speak the new first line.
            if hadFocus: ui.message(engine.first_line(message))
            else: dialog.message.SetFocus()
            return
        dialog = InformationDialog(gui.mainFrame, title, message); self._informationDialog = dialog
        def close(_event=None):
            if self._informationDialog is dialog: self._informationDialog = None
            dialog.Destroy()
        def onKey(event):
            key = event.GetKeyCode()
            if key == wx.WXK_ESCAPE or (key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and dialog.message.HasFocus()): close(); return
            event.Skip()
        dialog.okButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Bind(wx.EVT_CHAR_HOOK, onKey)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.message.SetFocus)
    def _showGitHubResult(self, message, title=None):
        self._showInformation(title or _("GitHub publishing result"), message)
    def _githubPreflight(self, ready):
        callback = None; arguments = ()
        try:
            reports = publisher.preflight(ready, engine.values, self._githubProgress, self._githubCancelled)
            self._rememberExistingReleases(reports)
            callback, arguments = self._offerGitHubPush, (reports, ready)
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerGitHubLogin, (ready,)
        except publisher.OperationCancelled:
            callback, arguments = self._showGitHubResult, (_("GitHub publishing was cancelled during the safety check. No changes were made."), _("GitHub publishing cancelled"))
        except Exception as error:
            log.exception("GitHub publishing preflight failed"); callback, arguments = self._showGitHubResult, (_("GitHub publishing preflight failed: %s.") % error, _("GitHub publishing failed"))
        finally:
            self._scanActive.clear(); self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerGitHubLogin(self, ready):
        self._showConfirmation(_("Sign in to GitHub"), _("GitHub CLI is not signed in for NVDA. Sign in now? A one-time code will be copied to the clipboard and GitHub will open in your browser. Press Enter or select Yes to continue."), lambda: self._startGitHubLogin(ready), defaultYes=True, onNo=lambda: self._showGitHubResult(_("GitHub publishing was canceled.")))
    def _startGitHubLogin(self, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Starting GitHub sign-in. The one-time code will be copied to the clipboard. Complete authorization in the browser.")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubLogin, args=(ready,), name="addonDeveloperGitHubLogin", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub login"); ui.message(_("GitHub sign-in could not be started"))
    def _githubLogin(self, ready):
        succeeded = False
        try:
            publisher.login(); succeeded = True; wx.CallAfter(ui.message, _("GitHub sign-in completed. Retrying the publishing safety check."))
        except publisher.GitHubCliRequired:
            wx.CallAfter(self._offerGitHubCliInstall)
        except Exception as error:
            log.exception("GitHub login failed"); wx.CallAfter(ui.message, _("GitHub sign-in failed: %s") % error)
        finally:
            self._stopProgress(); self._scanLock.release()
            if succeeded and not self._shutdown.is_set(): wx.CallAfter(self._beginGitHubPreflight, ready)
    def _offerGitHubCliInstall(self):
        self._showConfirmation(_("GitHub CLI required"), _("The optional GitHub features require GitHub CLI, but it is not installed or NVDA cannot find it. Open the official GitHub CLI installation page now? Installation is not automatic. After installing GitHub CLI, restart NVDA and run the command again."), self._openGitHubCliInstallationPage, defaultYes=True, onNo=lambda: self._showInformation(_("GitHub feature canceled"), _("GitHub CLI was not installed. No GitHub action was performed.")))
    def _openGitHubCliInstallationPage(self):
        def launch():
            try:
                if self._shutdown.wait(0.2): return
                os.startfile(publisher.GITHUB_CLI_URL)
            except OSError:
                log.exception("Could not open the GitHub CLI installation page")
                if not self._shutdown.is_set(): wx.CallAfter(ui.message, _("The GitHub CLI installation page could not be opened"))
        ui.message(_("Opening the official GitHub CLI installation page. After installation, restart NVDA and run the GitHub command again."))
        try: threading.Thread(target=launch, name="addonDeveloperOpenGitHubCli", daemon=True).start()
        except Exception:
            log.exception("Could not start the GitHub CLI installation-page launcher"); ui.message(_("The GitHub CLI installation page could not be opened"))
    def _offerGitHubPush(self, reports, ready=None):
        # Everything is gathered into the confirmation itself instead of being
        # spoken beforehand, so nothing is heard twice or cut off by the window.
        eligible, notOwned, versionConflicts = publisher.publish_plan(reports)
        skipped = [_("Skipped, the repository belongs to another GitHub account: %s") % publisher.public_name(report) for report in notOwned]
        increments = [(report, publisher.next_patch_version(report.version)) for report in versionConflicts]
        increments = [(report, version) for report, version in increments if version]
        if ready is not None and increments:
            summary = ngettext("Increase the version of %d add-on before publishing?", "Increase the versions of %d add-ons before publishing?", len(increments)) % len(increments)
            details = [ngettext("Why: this version is already released on GitHub, but your copy has changes that are not in that release.", "Why: these versions are already released on GitHub, but your copies have changes that are not in those releases.", len(increments))]
            details += [_("%s: %s becomes %s") % (publisher.public_name(report), report.version, version) for report, version in increments]
            details += [_("The manifest, any addon_version in buildVars.py, and readme version lines are changed."), _("Nothing is pushed yet. The publishing check runs again so you can review everything first."), _("Tip: add notes for the new version to CHANGELOG.md before you confirm publishing.")]
            message = engine.readable_message(summary, details, _("Yes increases the versions. No publishes the other add-ons without these."))
            self._showConfirmation(_("Increment add-on versions"), message, lambda: self._startVersionIncrement(increments, ready), onNo=lambda: self._offerGitHubPush(reports))
            return
        skipped += [_("Skipped, version %s is already released with different code: %s. Increase its version to publish it.") % (report.version, publisher.public_name(report)) for report in versionConflicts]
        reports = eligible
        if not reports:
            if versionConflicts:
                message = engine.readable_message(_("Nothing can be published yet."), skipped)
                self._showInformation(_("Manifest version increment required"), message); return
            message = engine.readable_message(_("Everything is already published. GitHub has the newest version of each selected add-on."), skipped)
            self._showInformation(_("GitHub releases are current"), message); return
        create = [publisher.public_name(report) for report in reports if not report.remote]; changed = sum(report.changed_files for report in reports)
        unpushed = sum(report.unpushed_commits for report in reports)
        summary = ngettext("Publish %d add-on to GitHub?", "Publish %d add-ons to GitHub?", len(reports)) % len(reports)
        details = [ngettext("Commit %d changed file.", "Commit %d changed files.", changed) % changed if changed else _("No files need committing."),
                   ngettext("Push %d local commit.", "Push %d local commits.", unpushed) % unpushed if unpushed else _("No earlier local commits need pushing.")]
        if create: details.append(_("Create a new public GitHub repository for: %s") % ", ".join(create))
        details.append("")
        for report in reports:
            released = getattr(report, "github_release_version", "") or _("no release yet")
            details.append(_("%s %s: GitHub has %s. Your branch: %s. Default branch on GitHub: %s.") % (publisher.public_name(report), report.version, released, getattr(report, "current_branch", "") or _("unknown"), getattr(report, "default_branch", "") or _("set after the first push")))
        withoutNotes = publisher.reports_without_release_notes(reports)
        if withoutNotes:
            details += ["", _("No release notes found for: %s") % ", ".join(_("%s %s") % (publisher.public_name(report), report.version) for report in withoutNotes),
                        _("Their GitHub Release would say only that it is a release package. To add notes, choose No, add a section named after the version to CHANGELOG.md, and publish again.")]
        if skipped: details += [""] + skipped
        details += ["", _("A GitHub Release is created only after its commit is on the repository's default branch.")]
        message = engine.readable_message(summary, details, _("Yes publishes. No changes nothing."))
        self._showConfirmation(_("Publish add-ons to GitHub"), message, lambda: self._startGitHubPush(reports), onNo=lambda: self._showGitHubResult(_("GitHub publishing was canceled. Nothing was changed.")))
    def _startVersionIncrement(self, increments, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Incrementing %d add-on versions") % len(increments)); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._incrementVersions, args=(increments, ready), name="addonDeveloperVersionIncrement", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start version increments"); ui.message(_("The add-on versions could not be incremented"))
    def _incrementVersions(self, increments, ready):
        changed = []; failed = []
        try:
            for report, version in increments:
                name = publisher.public_name(report)
                self._githubProgress(_("Incrementing %s from %s to %s") % (name, report.version, version))
                try:
                    publisher.increment_project_version(Path(report.manifest), Path(report.root), report.version, version); changed.append(_("%s %s") % (name, version))
                except Exception as error:
                    log.exception("Could not increment the version of %s", report.manifest); failed.append(_("%s: %s") % (name, error))
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishVersionIncrement, changed, failed, ready)
    def _finishVersionIncrement(self, changed, failed, ready):
        message = _("Versions incremented: %s.") % "; ".join(changed) if changed else _("No add-on versions were changed.")
        if failed: message += _(" Versions not changed: %s.") % "; ".join(failed)
        if not changed:
            self._showInformation(_("Version increment failed"), message); return
        # Keep this short: the progress window opens immediately afterward.
        spoken = _("Versions increased: %s. Checking again before publishing.") % "; ".join(changed)
        if failed: spoken += " " + ngettext("%d version could not be changed.", "%d versions could not be changed.", len(failed)) % len(failed)
        ui.message(spoken); self._beginGitHubPreflight(ready)
    def _rememberExistingReleases(self, reports):
        current = publisher.reports_with_current_releases(reports)
        if not current: return
        state = engine.read_json(self._statePath, {}); metadata = state.get("submissionMetadata", {}) if isinstance(state.get("submissionMetadata"), dict) else {}
        for report in current:
            metadata[report.project_id] = {**report.__dict__, "sourceUrl": publisher.repository_url(report.remote), "downloadUrl": report.github_release_download_url}
        state["submissionMetadata"] = metadata; engine.atomic_json_write(self._statePath, state)
    def _startGitHubPush(self, reports):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Publishing confirmed add-on projects to GitHub")); self._scanCancel.clear(); self._scanActive.set(); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubPush, args=(reports,), name="addonDeveloperGitHubPush", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub publishing"); ui.message(_("GitHub publishing could not be started"))
    def _githubPush(self, reports):
        callback = None; arguments = ()
        try:
            publisher.validate_publish_builds(reports, self._githubProgress, self._githubCancelled)
            published = publisher.push(reports, self._githubProgress, self._githubCancelled); callback, arguments = self._offerGitHubRelease, (published,)
        except publisher.OperationCancelled as cancellation:
            names = "; ".join(publisher.public_name(item) for item in cancellation.completed)
            message = _("GitHub publishing was cancelled after pushing: %s. The remaining add-ons were not changed. Run publishing again to create releases for the pushed add-ons.") % names if names else _("GitHub publishing was cancelled before any repository was changed.")
            callback, arguments = self._showGitHubResult, (message, _("GitHub publishing cancelled"))
        except Exception as error:
            log.exception("GitHub publishing failed"); callback, arguments = self._showGitHubResult, (_("GitHub publishing failed: %s.") % error, _("GitHub publishing failed"))
        finally:
            self._scanActive.clear(); self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerGitHubRelease(self, published, allowDefaultPromotion=True, announcePush=True):
        # The push result travels into the next window instead of being spoken
        # separately, where the window opening would cut it off.
        pushed = [_("Pushed to GitHub: %s") % ", ".join(publisher.public_name(item) for item in published)] if announcePush else []
        blocked = [item for item in published if item.get("releaseBlocker")]
        waiting = [_("%s: %s") % (publisher.public_name(item), item["releaseBlocker"]) for item in blocked]
        promotable = [item for item in blocked if item.get("defaultBranchCanFastForward") and publisher.release_publish_action(item)]
        if allowDefaultPromotion and promotable:
            summary = ngettext("Move %d release commit onto its default branch?", "Move %d release commits onto their default branches?", len(promotable)) % len(promotable)
            details = pushed + [_("Why: a GitHub Release must come from the repository's default branch, and these commits are only on your working branch.")]
            details += [_("%s: copy branch %s onto %s") % (publisher.public_name(item), item.get("currentBranch") or _("unknown branch"), item.get("defaultBranch") or _("unknown default branch")) for item in promotable]
            details += [_("This is safe: the default branch has no commits that would be lost, and your own checkout is not changed.")]
            message = engine.readable_message(summary, details, _("Yes updates the default branches, then asks about releases. No leaves only your working branches pushed."))
            self._showConfirmation(_("Update repository default branches"), message, lambda: self._startGitHubDefaultPromotion(published, promotable), onNo=lambda: self._offerGitHubRelease(published, False, False)); return
        differing = [item for item in published if not item.get("releaseBlocker") and publisher.release_publish_action(item)]
        if not differing:
            if blocked:
                self._offerGitHubPullRequests(blocked, pushed); return
            message = engine.readable_message(_("Push complete. GitHub already has the newest release of each add-on."), pushed)
            self._showInformation(_("GitHub releases are current"), message); return
        summary = ngettext("Create %d GitHub Release now?", "Create %d GitHub Releases now?", len(differing)) % len(differing)
        details = list(pushed)
        for item in differing:
            action = _("replace the missing or damaged package in its existing release") if publisher.release_publish_action(item) == "repair" else _("new release")
            details.append(_("%s %s: %s. GitHub currently has %s.") % (publisher.public_name(item), item.get("version") or _("unknown version"), action, item.get("github_release_version") or _("no release")))
        if blocked: details += ["", _("Waiting for the default branch, so no release yet:")] + waiting
        message = engine.readable_message(summary, details, _("Yes creates the releases. No leaves your code pushed without new releases."))
        onNo = (lambda: self._offerGitHubPullRequests(blocked, [_("No new GitHub Releases were created.")])) if blocked else (lambda: self._showGitHubResult(_("Your code was pushed, but no new GitHub Release was created.")))
        self._showConfirmation(_("Release new add-on versions"), message, lambda: self._startGitHubRelease(differing, blocked), onNo=onNo)
    def _startGitHubDefaultPromotion(self, published, promotable):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Updating confirmed repository default branches")); self._scanCancel.clear(); self._scanActive.set(); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubDefaultPromotion, args=(published, promotable), name="addonDeveloperGitHubDefaultPromotion", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub default-branch update"); ui.message(_("The repository default-branch update could not be started"))
    def _githubDefaultPromotion(self, published, promotable):
        callback = None; arguments = ()
        try:
            updated = publisher.promote_default_branches(promotable, self._githubProgress, self._githubCancelled)
            byProject = {item["project_id"]: item for item in updated}
            published = [byProject.get(item["project_id"], item) for item in published]
            callback, arguments = self._offerGitHubRelease, (published, False, False)
        except publisher.OperationCancelled as cancellation:
            names = "; ".join(publisher.public_name(item) for item in cancellation.completed)
            message = _("Default-branch updates were cancelled after checking: %s. No release was created.") % names if names else _("Default-branch updates were cancelled before any default branch changed. No release was created.")
            callback, arguments = self._showGitHubResult, (message, _("GitHub publishing cancelled"))
        except Exception as error:
            log.exception("GitHub default-branch update failed"); callback, arguments = self._showGitHubResult, (_("GitHub default-branch update failed: %s. No release was created.") % error, _("GitHub publishing failed"))
        finally:
            self._scanActive.clear(); self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerGitHubPullRequests(self, blocked, context=()):
        context = [context] if isinstance(context, str) else list(context)
        waiting = [_("%s: %s") % (publisher.public_name(item), item["releaseBlocker"]) for item in blocked]
        available = [(item, publisher.pull_request_url(item)) for item in blocked]
        available = [(item, url) for item, url in available if url]
        if not available:
            message = engine.readable_message(_("Your code was pushed, but some releases have to wait."), context + ["", _("Why each release is waiting:")] + waiting)
            self._showInformation(_("GitHub release deferred"), message); return
        summary = ngettext("Open %d GitHub page to merge a pull request?", "Open %d GitHub pages to merge pull requests?", len(available)) % len(available)
        details = context + ["", _("Your code was pushed, but these releases must wait until the code is merged into the default branch:")] + waiting
        details += ["", _("Each page lets you create a pull request. After merging it, run publishing again to create the release.")]
        message = engine.readable_message(summary, details, _("Yes opens the pages in your browser. No leaves your working branches pushed."))
        self._showConfirmation(_("Open pull requests for deferred releases"), message, lambda: self._openGitHubPullRequestPages(available), onNo=lambda: self._showGitHubResult(_("Your working branches remain pushed. No release or pull request was created.")))
    def _openGitHubPullRequestPages(self, repositories):
        names = "; ".join(publisher.public_name(item) for item, _url in repositories)
        def launch():
            try:
                if self._shutdown.wait(0.2): return
                for _item, url in repositories: os.startfile(url)
            except OSError:
                log.exception("Could not open a GitHub pull-request comparison page")
                if not self._shutdown.is_set(): wx.CallAfter(ui.message, _("One or more GitHub pull-request pages could not be opened"))
        ui.message(_("Opening GitHub comparison pages for: %s. Review and merge each pull request, then run the publishing command again.") % names)
        try: threading.Thread(target=launch, name="addonDeveloperOpenPullRequests", daemon=True).start()
        except Exception:
            log.exception("Could not start GitHub pull-request page launcher"); ui.message(_("The GitHub pull-request pages could not be opened"))
    def _startGitHubRelease(self, published, blocked=None):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Packaging and publishing GitHub Releases")); self._scanCancel.clear(); self._scanActive.set(); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubRelease, args=(published, blocked or []), name="addonDeveloperGitHubRelease", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub release publishing"); ui.message(_("GitHub release publishing could not be started"))
    def _githubRelease(self, published, blocked):
        callback = None; arguments = ()
        try:
            output = Path.home() / "Documents" / "NVDA Add-on Development" / "Packages"
            state = engine.read_json(self._statePath, {}); channels = state.get("submissionChannels", {}) if isinstance(state.get("submissionChannels"), dict) else {}
            publisher.resolve_release_channels(published, channels)
            metadata = state.get("submissionMetadata", {}) if isinstance(state.get("submissionMetadata"), dict) else {}
            def checkpoint(item):
                metadata[item["project_id"]] = item
                state["submissionMetadata"] = metadata
                engine.atomic_json_write(self._statePath, state)
            released = publisher.release(published, output, self._githubProgress, checkpoint, self._githubCancelled)
            names = "; ".join(publisher.public_name(item) for item in released)
            if blocked:
                callback, arguments = self._offerGitHubPullRequests, (blocked, _("GitHub Releases were published and store submission details saved for: %s. ") % names)
            else:
                callback, arguments = self._showGitHubResult, (_("GitHub Releases published and store submission details saved for: %s.") % names, _("GitHub publishing complete"))
        except publisher.OperationCancelled as cancellation:
            names = "; ".join(publisher.public_name(item) for item in cancellation.completed)
            message = _("GitHub release publishing was cancelled. Released and saved for store submission: %s. The remaining releases were not created; run publishing again to create them.") % names if names else _("GitHub release publishing was cancelled before any release was created. Run publishing again to create them.")
            callback, arguments = self._showGitHubResult, (message, _("GitHub publishing cancelled"))
        except Exception as error:
            log.exception("GitHub release publishing failed"); callback, arguments = self._showGitHubResult, (_("GitHub release publishing failed: %s.") % error, _("GitHub publishing failed"))
        finally:
            self._scanActive.clear(); self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _githubCancelled(self):
        return self._scanCancel.is_set() or self._shutdown.is_set()
    def _githubProgress(self, message):
        if not self._shutdown.is_set(): wx.CallAfter(self._updateProgressMessage, message)
    def _startSubmissionLauncher(self, results, urls):
        try:
            threading.Thread(target=self._openSubmissionResources, args=(results, urls), name="addonDeveloperOpenResources", daemon=True).start()
        except Exception:
            log.exception("Could not start add-on resource launcher")
            ui.message(_("The add-on folders could not be opened"))
    def _openSubmissionResources(self, results, urls):
        if self._shutdown.wait(1.5): return
        folders = list(dict.fromkeys(str(Path(result.path).parent) for result in results))
        try:
            for folder in folders: os.startfile(folder)
            for url in urls: os.startfile(url)
        except OSError:
            log.exception("Could not open add-on submission resources")
            if not self._shutdown.is_set(): wx.CallAfter(ui.message, _("One or more add-on folders or the submission form could not be opened"))
    @staticmethod
    def _discoveryLimitText(completeDepth):
        """Explain how far a breadth-first discovery searched before its safety limit."""
        if isinstance(completeDepth, int) and completeDepth >= 0:
            return _("The folder search reached its limit after fully searching %d folder levels below each development folder. Projects deeper than that may have been missed; add their folders in settings.") % completeDepth
        return _("The folder search reached its time or folder limit. If a project was missed, add its folder in settings.")
    def _finishManualCheck(self, results, awaiting, release, sameCompatibilityFamily, discoveryTruncated=False, completeDepth=None):
        # The outcome comes first; notes about how the scan ran follow it.
        notes = []
        if sameCompatibilityFamily: notes.append(_("NVDA %s uses the same compatibility target as before, %s, so this build alone needs no manifest change.") % (release.tag, release.manifest_version))
        if release.source == "cached": notes.append(_("NVDA's release website could not be reached, so the last known release information was used."))
        if discoveryTruncated: notes.append(self._discoveryLimitText(completeDepth))
        message = engine.readable_text(self._completionMessage(results)) + ("\n\n" + "\n".join(notes) if notes else "")
        if awaiting:
            # The approval window opens next, so only the first line is spoken.
            ui.message(engine.first_line(message)); self._showProjectApprovalDialog(awaiting, release)
        else: self._showInformation(_("Scan results"), message)
    @staticmethod
    def _checkErrorText(error):
        detail = " ".join(str(error).split())[:300] or error.__class__.__name__
        lowered = detail.casefold()
        if any(word in lowered for word in ("url", "http", "network", "timed out", "name resolution", "connection")):
            return _("NVDA release services could not be reached: %s") % detail
        if "worker" in lowered or "powershell" in lowered:
            return _("The isolated project-discovery worker failed: %s") % detail
        return _("The update check failed: %s") % detail
    def _runCheck(self, manual=False, full_system=False, lock_acquired=False, selectedRelease=None):
        if not lock_acquired and not self._scanLock.acquire(blocking=False): return
        self._scanActive.set(); progressStarted = False; progressEvent = None; finishCallback = None
        cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set()
        requestPath = self._statePath.with_name(f"addonDeveloperUpdaterRequest-{uuid.uuid4().hex}.json")
        outputPath = self._statePath.with_name(f"addonDeveloperUpdaterResult-{uuid.uuid4().hex}.json")
        try:
            settings = config.conf["addonDeveloperUpdater"]; state = engine.read_json(self._statePath, {})
            mode = engine.worker_mode(manual, state, settings["periodicRescanHours"])
            if manual or mode == "periodic":
                self._startProgress(); progressStarted = True; progressEvent = self._progressStop; wx.CallAfter(self._setProgressMessage, _("Checking official NVDA release information"))
            release = selectedRelease or engine.latest_release(bool(settings["includePrereleases"]), self._releaseCachePath)
            if progressStarted: wx.CallAfter(self._setProgressMessage, _("Discovering add-on development projects in the isolated worker"))
            storedPaths = [str(path) for path in engine.approved_manifest_paths(state)]
            checkedAt = engine.utc_now(); previousAutomaticError = str(state.get("lastAutomaticError", "")); previousReleaseSource = str(state.get("releaseSource", ""))
            if not manual and state.get("releaseTag") == release.tag and not engine.rescan_due(state, settings["periodicRescanHours"]):
                state.update({"lastCheckAt": checkedAt, "lastSuccessfulCheckAt": checkedAt, "lastCheckError": "", "lastAutomaticError": "", "automaticFailureCount": 0, "releaseSource": release.source, "releaseCheckedAt": release.checked_at, "releaseBuildVersion": release.build_version, "prerelease": release.prerelease, "releaseUrl": release.url})
                engine.atomic_json_write(self._statePath, state)
                if previousAutomaticError: wx.CallAfter(ui.message, _("Automatic NVDA add-on update checks are working again"))
                if release.source == "cached" and previousReleaseSource != "cached": wx.CallAfter(ui.message, _("Live NVDA release services are unavailable. Automatic checks are using the last known release information."))
                elif release.source != "cached" and previousReleaseSource == "cached": wx.CallAfter(ui.message, _("Live NVDA release information is available again"))
                return
            request = {"mode": mode, "fullSystem": bool(full_system), "roots": self._rootStrings(), "manifestPaths": storedPaths}
            engine.atomic_json_write(requestPath, request)
            workerPath = Path(__file__).with_name("worker.ps1")
            if not workerPath.is_file(): raise RuntimeError("The external scan worker is missing. Reinstall Add-on Developer Updater.")
            command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(workerPath), "-RequestPath", str(requestPath), "-OutputPath", str(outputPath)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            self._workerProcess = subprocess.Popen(command, creationflags=flags)
            try:
                returnCode = engine.wait_for_worker(self._workerProcess)
            finally: self._workerProcess = None
            workerResult = engine.read_json(outputPath, {})
            if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
            if returnCode or not workerResult.get("ok"):
                detail = workerResult.get("error")
                if not detail: detail = f"External worker stopped before returning details (Windows status 0x{returnCode & 0xFFFFFFFF:08X})"
                raise RuntimeError(detail)
            manifests = [Path(path) for path in workerResult.get("manifests", []) if isinstance(path, str)]
            discoveryTruncated = bool(workerResult.get("truncated")); completeDepth = workerResult.get("completeDepth") if isinstance(workerResult.get("completeDepth"), int) else None; previousDiscoveryTruncated = bool(state.get("lastDiscoveryTruncated"))
            if progressStarted: wx.CallAfter(self._setProgressMessage, _("Checking %d discovered add-on manifests") % len(manifests))
            approved = engine.approved_project_ids(state)
            if "approvedProjects" not in state:
                for item in state.get("projects", []) if isinstance(state.get("projects"), list) else []:
                    if not isinstance(item, dict) or not item.get("path"): continue
                    try: approved.add(engine.project_id(Path(item["path"])))
                    except (OSError, TypeError, ValueError): pass
            results = engine.unavailable_manifest_results(workerResult.get("unavailableManifestPaths", []), state.get("projects", []))
            for index, path in enumerate(manifests, 1):
                if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
                if progressStarted: wx.CallAfter(self._setProgressStep, progressEvent, _("Checking add-on manifest %d of %d: %s") % (index, len(manifests), path.parent.name), index, len(manifests))
                identifier = engine.project_id(path)
                try:
                    if identifier not in approved:
                        # Git details are gathered here, on the worker thread, so the tracking window can tell copies apart.
                        metadata = engine.values(path); results.append(engine.add_copy_details(engine.ProjectResult(identifier, metadata.get("name", path.parent.name), str(path), "awaiting manual approval"), path)); continue
                    results.append(self._addProjectContext(engine.update(path, release, self._backupRoot, False, cancelled), path))
                except Exception as error:
                    log.exception("Could not process add-on manifest %s", path)
                    results.append(engine.ProjectResult(identifier, path.parent.name, str(path), f"validation failed: {error}"))
                if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
            projects = engine.merge_project_records(state.get("projects", []), results); ignored = engine.ignored_project_ids({"projects": projects, "ignoredProjects": state.get("ignoredProjects", [])})
            visibleResults = [result for result in results if result.project_id not in ignored]
            previousRecords = {item.get("project_id"): item for item in state.get("projects", []) if isinstance(item, dict) and item.get("project_id")}
            previousUpdates = state.get("notifiedUpdates", {}) if isinstance(state.get("notifiedUpdates"), dict) else {}; previousAwaiting = state.get("notifiedAwaiting", {}) if isinstance(state.get("notifiedAwaiting"), dict) else {}; previousFailures = state.get("notifiedFailures", {}) if isinstance(state.get("notifiedFailures"), dict) else {}
            allVisibleRecords = engine.visible_project_records({"projects": projects, "ignoredProjects": sorted(ignored)})[0]
            currentUpdates, currentAwaiting, currentFailures, changedNotifications, resolvedFailureIds = engine.notification_state(allVisibleRecords, previousUpdates, previousAwaiting, previousFailures)
            newlyActionable = [result for result in visibleResults if result.project_id in changedNotifications]
            resolvedFailures = [previousRecords[identifier].get("name", identifier) for identifier in resolvedFailureIds if identifier in previousRecords]
            sameCompatibilityFamily = bool(state.get("releaseTag") and state.get("releaseTag") != release.tag and state.get("manifestVersion") == release.manifest_version)
            state.update({"releaseTag": release.tag, "releaseBuildVersion": release.build_version, "manifestVersion": release.manifest_version, "prerelease": release.prerelease, "releaseUrl": release.url, "releaseSource": release.source, "releaseCheckedAt": release.checked_at, "lastCheckAt": checkedAt, "lastSuccessfulCheckAt": checkedAt, "lastScanAt": checkedAt, "lastCheckError": "", "lastAutomaticError": "", "automaticFailureCount": 0, "approvedProjects": sorted(approved), "ignoredProjects": sorted(ignored), "projects": projects, "notifiedUpdates": currentUpdates, "notifiedAwaiting": currentAwaiting, "notifiedFailures": currentFailures})
            if manual and selectedRelease is not None:
                state["manualReleaseTargetTag"] = release.tag; engine.save_manual_review(state, release, visibleResults)
            if mode != "background": state.update({"lastDiscoveryAt": checkedAt, "lastDiscoveryDirectories": int(workerResult.get("directoriesVisited", 0)), "lastDiscoverySeconds": workerResult.get("elapsedSeconds", 0), "lastDiscoveryTruncated": discoveryTruncated, "lastDiscoveryCompleteDepth": completeDepth})
            engine.atomic_json_write(self._statePath, state)
            for result in results:
                if result.status != "current": log.info("Add-on Developer Updater: %s: %s", result.path, result.status)
            awaiting = [result for result in visibleResults if "awaiting" in result.status]
            if manual: finishCallback = (self._finishManualCheck, visibleResults, awaiting, release, sameCompatibilityFamily, discoveryTruncated, completeDepth)
            else:
                if newlyActionable:
                    # A short tone makes a background result noticeable even when its speech is missed.
                    if settings["notificationSound"]: wx.CallAfter(tones.beep, 880, 80)
                    wx.CallAfter(ui.message, self._completionMessage(newlyActionable))
                if resolvedFailures: wx.CallAfter(ui.message, _("Add-on validation is working again for: %s") % "; ".join(resolvedFailures))
                if previousAutomaticError: wx.CallAfter(ui.message, _("Automatic NVDA add-on update checks are working again"))
                if release.source == "cached" and previousReleaseSource != "cached": wx.CallAfter(ui.message, _("Live NVDA release services are unavailable. Automatic checks are using the last known release information."))
                elif release.source != "cached" and previousReleaseSource == "cached": wx.CallAfter(ui.message, _("Live NVDA release information is available again"))
                if discoveryTruncated and not previousDiscoveryTruncated: wx.CallAfter(ui.message, self._discoveryLimitText(completeDepth))
        except Exception as error:
            message = self._checkErrorText(error); state = engine.read_json(self._statePath, {}); previousError = str(state.get("lastAutomaticError", "")); state["lastCheckAt"] = engine.utc_now(); state["lastCheckError"] = message
            if not manual:
                state["lastAutomaticError"] = message; state["automaticFailureCount"] = min(10, int(state.get("automaticFailureCount", 0) or 0) + 1)
            try: engine.atomic_json_write(self._statePath, state)
            except OSError: pass
            if manual or previousError != message:
                log.exception("Add-on Developer Updater check failed"); wx.CallAfter(ui.message, message)
            else: log.debug("Repeated automatic update-check failure suppressed: %s", message)
        finally:
            self._workerProcess = None
            for path in (requestPath, outputPath):
                try: path.unlink(missing_ok=True)
                except OSError: pass
            self._scanActive.clear(); self._scanLock.release()
            if finishCallback is not None:
                if progressStarted: self._finishProgressThen(*finishCallback)
                else: wx.CallAfter(finishCallback[0], *finishCallback[1:])
            elif progressStarted: self._stopProgress()
