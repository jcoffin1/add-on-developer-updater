from __future__ import annotations

import os, subprocess, threading, uuid
from datetime import datetime, timezone
from pathlib import Path

import addonHandler, config, globalPluginHandler, globalVars, gui, scriptHandler, ui, wx
from gui.nvdaControls import CustomCheckListBox
from gui.settingsDialogs import SettingsPanel
from logHandler import log
from . import engine, publisher

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
}

class UpdaterSettingsPanel(SettingsPanel):
    title = SCRIPT_CATEGORY
    def makeSettings(self, settingsSizer):
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer); settings = config.conf["addonDeveloperUpdater"]
        self.automatic = helper.addItem(wx.CheckBox(self, label=_("Check automatically in the background"))); self.automatic.SetValue(settings["automaticChecks"])
        self.prereleases = helper.addItem(wx.CheckBox(self, label=_("Include alpha, beta, and release candidate builds"))); self.prereleases.SetValue(settings["includePrereleases"])
        self.fixed = helper.addItem(wx.CheckBox(self, label=_("Include all accessible drives during manual scans"))); self.fixed.SetValue(settings["scanFixedDrives"])
        self.interval = helper.addLabeledControl(_("Release check interval in minutes:"), wx.SpinCtrl, min=15, max=1440, initial=max(15, settings["intervalMinutes"]))
        self.rescan = helper.addLabeledControl(_("Recheck approved projects every number of hours:"), wx.SpinCtrl, min=1, max=720, initial=settings["periodicRescanHours"])
        self.roots = helper.addLabeledControl(_("Additional development folders, separated by semicolons:"), wx.TextCtrl, value=settings["scanRoots"])
    def onSave(self):
        settings = config.conf["addonDeveloperUpdater"]
        settings["automaticChecks"] = self.automatic.IsChecked(); settings["includePrereleases"] = self.prereleases.IsChecked()
        settings["scanFixedDrives"] = self.fixed.IsChecked()
        settings["intervalMinutes"] = self.interval.GetValue(); settings["periodicRescanHours"] = self.rescan.GetValue(); settings["scanRoots"] = self.roots.GetValue()
        if GlobalPlugin.activeInstance is not None: GlobalPlugin.activeInstance._settingsChanged()

class UpdateReviewDialog(wx.Dialog):
    def __init__(self, parent, results):
        super().__init__(parent, title=_("Review NVDA add-on compatibility updates"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = results; mainSizer = wx.BoxSizer(wx.VERTICAL)
        self.onUpdate = None; self.allSelectedMessage = _("All add-on updates selected")
        mainSizer.Add(wx.StaticText(self, label=_("Check the add-ons whose manifests should be updated. Nothing is selected by default. Press Space to check an item, Control+A to select all, Enter to update the checked items, or Escape to close.")), 0, wx.ALL, 10)
        choices = [_("%s: NVDA %s; %s") % (result.name, result.status.removeprefix("update available: "), result.path) for result in results]
        self.checkList = CustomCheckListBox(self, choices=choices)
        mainSizer.Add(self.checkList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        selectionSizer = wx.BoxSizer(wx.HORIZONTAL)
        selectAll = wx.Button(self, label=_("Select &all")); selectNone = wx.Button(self, label=_("Select &none"))
        selectAll.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(True)); selectNone.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(False))
        selectionSizer.Add(selectAll, 0, wx.RIGHT, 8); selectionSizer.Add(selectNone, 0)
        mainSizer.Add(selectionSizer, 0, wx.ALL, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.updateButton = wx.Button(self, label=_("&Update selected")); self.closeButton = wx.Button(self, label=_("&Close"))
        self.updateButton.SetDefault()
        buttons.Add(self.updateButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0)
        mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((600, 320)); self.SetSize((800, 450)); self.CentreOnScreen()
        self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def _setAll(self, checked):
        for index in range(len(self.results)): self.checkList.Check(index, checked)
    def selectedResults(self):
        return [result for index, result in enumerate(self.results) if self.checkList.IsChecked(index)]
    def _onKey(self, event):
        key = event.GetKeyCode()
        listFocused = event.GetEventObject() is self.checkList or self.checkList.HasFocus()
        if event.ControlDown() and key in (ord("A"), ord("a")):
            self._setAll(True); ui.message(self.allSelectedMessage); return
        if key == wx.WXK_ESCAPE:
            self.Close(); return
        if listFocused and key == wx.WXK_SPACE:
            index = self.checkList.GetSelection()
            if index != wx.NOT_FOUND:
                checked = not self.checkList.IsChecked(index)
                self.checkList.Check(index, checked)
                ui.message(_("Selected") if checked else _("Not selected"))
            return
        if listFocused and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onUpdate is not None: self.onUpdate(None)
            return
        event.Skip()

class GitHubPublishDialog(UpdateReviewDialog):
    def __init__(self, parent, results):
        wx.Dialog.__init__(self, parent, title=_("Select add-ons to publish to GitHub"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = results; self.onUpdate = None; self.allSelectedMessage = _("All GitHub projects selected"); mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=_("Check the add-on projects to inspect and publish. Nothing is selected by default. Press Space to check an item, Control+A to select all, Enter to continue, or Escape to close.")), 0, wx.ALL, 10)
        labels = {"primary": _("primary development copy"), "codex": _("Codex work copy"), "other": _("development copy")}
        self.checkList = CustomCheckListBox(self, choices=[_("%s; %s; %s") % (result.name, labels[engine.path_kind(result.path)], result.path) for result in results])
        mainSizer.Add(self.checkList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        selectionSizer = wx.BoxSizer(wx.HORIZONTAL); selectAll = wx.Button(self, label=_("Select &all")); selectNone = wx.Button(self, label=_("Select &none"))
        selectAll.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(True)); selectNone.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(False)); selectionSizer.Add(selectAll, 0, wx.RIGHT, 8); selectionSizer.Add(selectNone, 0)
        mainSizer.Add(selectionSizer, 0, wx.ALL, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.publishButton = wx.Button(self, label=_("&Continue")); self.closeButton = wx.Button(self, label=_("&Close")); self.publishButton.SetDefault()
        buttons.Add(self.publishButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((600, 320)); self.SetSize((800, 450)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)

class StoreSubmissionDialog(GitHubPublishDialog):
    def __init__(self, parent, results):
        super().__init__(parent, results)
        self.SetTitle(_("Select add-ons to check for store submission"))
        self.allSelectedMessage = _("All store-submission projects selected")
        self.publishButton.SetLabel(_("&Check selected"))

class DowngradeDialog(GitHubPublishDialog):
    def __init__(self, parent, results):
        wx.Dialog.__init__(self, parent, title=_("Downgrade add-on compatibility versions"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = results; self.onUpdate = None; self.allSelectedMessage = _("All downgrade projects selected"); mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=_("Enter an older NVDA compatibility version, then check the add-ons to downgrade. Nothing is selected by default.")), 0, wx.ALL, 10)
        targetSizer = wx.BoxSizer(wx.HORIZONTAL); targetSizer.Add(wx.StaticText(self, label=_("Target NVDA version:")), 0, wx.RIGHT, 8); self.target = wx.TextCtrl(self); targetSizer.Add(self.target, 1, wx.EXPAND); mainSizer.Add(targetSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.checkList = CustomCheckListBox(self, choices=[_("%s; %s; %s") % (result.name, result.status, result.path) for result in results]); mainSizer.Add(self.checkList, 1, wx.EXPAND | wx.ALL, 10)
        selectionSizer = wx.BoxSizer(wx.HORIZONTAL); selectAll = wx.Button(self, label=_("Select &all")); selectNone = wx.Button(self, label=_("Select &none")); selectAll.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(True)); selectNone.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(False)); selectionSizer.Add(selectAll, 0, wx.RIGHT, 8); selectionSizer.Add(selectNone, 0); mainSizer.Add(selectionSizer, 0, wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.downgradeButton = wx.Button(self, label=_("&Downgrade selected")); self.closeButton = wx.Button(self, label=_("&Close")); self.downgradeButton.SetDefault(); buttons.Add(self.downgradeButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((650, 360)); self.SetSize((850, 500)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)

class ConfirmationDialog(wx.Dialog):
    def __init__(self, parent, title, message, defaultYes=False):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL); sizer.Add(wx.StaticText(self, label=message), 1, wx.EXPAND | wx.ALL, 15)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.yesButton = wx.Button(self, label=_("&Yes")); self.noButton = wx.Button(self, label=_("&No"))
        (self.yesButton if defaultYes else self.noButton).SetDefault(); self.defaultButton = self.yesButton if defaultYes else self.noButton
        buttons.Add(self.yesButton, 0, wx.RIGHT, 8); buttons.Add(self.noButton, 0); sizer.Add(buttons, 0, wx.ALL, 15)
        self.SetSizer(sizer); self.SetMinSize((600, 280)); self.SetSize((720, 360)); self.CentreOnScreen()

class InformationDialog(wx.Dialog):
    def __init__(self, parent, title, message):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL); self.message = wx.TextCtrl(self, value=message, style=wx.TE_READONLY | wx.TE_MULTILINE)
        sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 15); self.okButton = wx.Button(self, label=_("&OK")); self.okButton.SetDefault(); sizer.Add(self.okButton, 0, wx.ALL, 15)
        self.SetSizer(sizer); self.SetMinSize((550, 240)); self.SetSize((680, 300)); self.CentreOnScreen()

class OperationProgressDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title=_("Add-on Developer Updater progress"), style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.TextCtrl(self, value=_("An add-on developer operation is running. The repeating percentage indicates activity, not estimated completion."), style=wx.TE_READONLY)
        sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 10)
        self.gauge = wx.Gauge(self, range=100); self.gauge.SetValue(0); sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10); self.SetSizerAndFit(sizer)

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    activeInstance = None
    def __init__(self):
        super().__init__(); self._shutdown = threading.Event(); self._scanCancel = threading.Event(); self._wakeMonitor = threading.Event(); self._scanLock = threading.Lock(); self._progressStop = threading.Event(); self._progressThread = None; self._progressDialog = None; self._progressValue = 0; self._reviewDialog = None; self._publishDialog = None; self._downgradeDialog = None; self._confirmDialog = None; self._informationDialog = None; self._workerProcess = None
        config_path = Path(globalVars.appArgs.configPath); self._statePath = config_path / "addonDeveloperUpdaterState.json"; self._releaseCachePath = config_path / "addonDeveloperUpdaterReleaseCache.json"; self._backupRoot = config_path / "addonDeveloperUpdaterBackups"
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
        self._stopProgress(); stopEvent = threading.Event(); self._progressStop = stopEvent; self._progressValue = 0; wx.CallAfter(self._showProgress, stopEvent, keepFocus)
        def pulse():
            while not stopEvent.wait(2) and not self._shutdown.is_set(): wx.CallAfter(self._pulseProgress, stopEvent)
        self._progressThread = threading.Thread(target=pulse, name="addonDeveloperProgress", daemon=True); self._progressThread.start()
    def _showProgress(self, stopEvent, keepFocus=False):
        if stopEvent.is_set() or self._shutdown.is_set(): return
        if self._progressDialog is not None: self._progressDialog.Destroy()
        self._progressDialog = OperationProgressDialog(gui.mainFrame)
        if keepFocus:
            self._progressDialog.Show(); self._progressDialog.Raise(); self._progressDialog.status.SetFocus()
        else: self._progressDialog.ShowWithoutActivating()
    def _updateProgressMessage(self, message):
        if self._progressDialog is not None:
            self._progressDialog.status.SetValue(_(message)); self._progressDialog.Layout()
        ui.message(_(message))
    def _pulseProgress(self, stopEvent):
        if stopEvent is self._progressStop and not stopEvent.is_set() and self._progressDialog is not None:
            self._progressValue = self._progressValue + 10 if self._progressValue < 90 else 10
            self._progressDialog.gauge.SetValue(self._progressValue)
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
        if self._progressDialog is not None: self._progressDialog.Destroy(); self._progressDialog = None
        if self._confirmDialog is not None:
            self._confirmDialog.Raise(); self._confirmDialog.defaultButton.SetFocus()
        elif self._informationDialog is not None:
            self._informationDialog.Raise(); self._informationDialog.okButton.SetFocus()
    def terminate(self):
        self._shutdown.set(); self._scanCancel.set(); self._wakeMonitor.set(); self._stopProgress()
        if self._workerProcess is not None:
            try: self._workerProcess.terminate()
            except OSError: pass
        if self._reviewDialog is not None:
            self._reviewDialog.Destroy(); self._reviewDialog = None
        if self._publishDialog is not None:
            self._publishDialog.Destroy(); self._publishDialog = None
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
    @scriptHandler.script(description=_("Downgrade selected add-on compatibility versions"), gesture="kb:NVDA+alt+shift+d", category=SCRIPT_CATEGORY)
    def script_downgradeAddonCompatibility(self, gesture):
        state = engine.read_json(self._statePath, {}); approved = engine.approved_project_ids(state); records, _ignored = engine.visible_project_records(state); projects = []
        records, older = engine.newest_local_project_records(records)
        for item in records:
            if not isinstance(item, dict) or item.get("project_id") not in approved or not item.get("path"): continue
            try:
                path = Path(item["path"]); metadata = engine.values(path)
                status = _("last tested with NVDA %s; minimum NVDA %s") % (metadata["lasttestednvdaversion"], metadata["minimumnvdaversion"])
                projects.append(engine.ProjectResult(item["project_id"], metadata.get("name", item.get("name", path.parent.name)), str(path), status))
            except (KeyError, OSError, ValueError): pass
        if not projects: ui.message(_("No approved add-on development projects are available to downgrade")); return
        self._showDowngradeDialog(projects)
    def _showDowngradeDialog(self, projects):
        if self._downgradeDialog is not None: self._downgradeDialog.Raise(); return
        dialog = DowngradeDialog(gui.mainFrame, projects); self._downgradeDialog = dialog
        def close(_event=None):
            if self._downgradeDialog is dialog: self._downgradeDialog = None
            dialog.Destroy()
        def apply(_event=None):
            target = dialog.target.GetValue().strip(); selected = dialog.selectedResults()
            try: engine.version_tuple(target)
            except ValueError: ui.message(_("Enter a valid NVDA version such as 2026.2")); return
            if not selected: ui.message(_("No add-on projects were selected for downgrade")); return
            close(); self._startDowngrade(selected, target)
        dialog.onUpdate = apply; dialog.downgradeButton.Bind(wx.EVT_BUTTON, apply); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.target.SetFocus)
    def _startDowngrade(self, selected, target):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Downgrading %d selected add-on manifests to NVDA %s") % (len(selected), target)); self._startProgress()
        try: threading.Thread(target=self._downgradeSelected, args=(selected, target), name="addonDeveloperDowngrade", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start downgrade"); ui.message(_("The compatibility downgrade could not be started"))
    def _downgradeSelected(self, selected, target):
        results = []
        try:
            for item in selected:
                try: results.append(engine.downgrade(Path(item.path), target, self._backupRoot))
                except Exception as error: results.append(engine.ProjectResult(item.project_id, item.name, item.path, f"validation failed: {error}"))
            for result in results: log.info("Add-on Developer Updater downgrade: %s: %s", result.path, result.status)
            state = engine.read_json(self._statePath, {}); state["projects"] = engine.merge_project_records(state.get("projects", []), results); engine.atomic_json_write(self._statePath, state)
        finally:
            self._scanLock.release()
            self._finishProgressThen(self._finishDowngrade, results)
    def _finishDowngrade(self, results):
        changed = [item for item in results if item.status.startswith("downgraded from ")]; failed = [item for item in results if item not in changed]
        message = _("Downgrade complete. %d manifests changed: %s.") % (len(changed), self._limitedDetails(changed, lambda item: _("%s %s") % (item.name, item.status))) if changed else _("Downgrade complete. No manifests were changed.")
        if failed: message += _(" %d projects were not changed: %s.") % (len(failed), "; ".join(_("%s: %s") % (item.name, item.status.removeprefix("validation failed: ")) for item in failed))
        ui.message(message); self._showInformation(_("Compatibility downgrade results"), message)
    @scriptHandler.script(description=_("Review previously discovered NVDA add-on compatibility updates"), category=SCRIPT_CATEGORY)
    def script_reviewAddonProjectUpdates(self, gesture):
        state = engine.read_json(self._statePath, {}); available = []
        for item in engine.visible_project_records(state)[0]:
            if isinstance(item, dict) and str(item.get("status", "")).startswith("update available: "):
                try: available.append(engine.ProjectResult(**item))
                except TypeError: pass
        if not available: ui.message(_("No saved add-on compatibility updates are available")); return
        self._showReviewDialog(available, engine.Release(str(state.get("releaseTag", "")), str(state.get("manifestVersion", "")), True, ""))
    @scriptHandler.script(description=_("Cancel the running add-on developer update scan"), category=SCRIPT_CATEGORY)
    def script_cancelAddonProjectScan(self, gesture):
        if not self._scanLock.locked(): ui.message(_("No add-on developer scan is running")); return
        self._scanCancel.set()
        if self._workerProcess is not None:
            try: self._workerProcess.terminate()
            except OSError: pass
        ui.message(_("Add-on developer scan cancellation requested"))
    @scriptHandler.script(description=_("Submit updated add-ons to the NVDA Add-on Store"), gesture="kb:NVDA+alt+shift+s", category=SCRIPT_CATEGORY)
    def script_submitUpdatedAddons(self, gesture):
        ready = self._approvedProjectResults(engine.read_json(self._statePath, {}))
        if not ready: ui.message(_("No approved add-on development projects are available to check for store submission")); return
        self._showStoreSubmissionDialog(ready)
    def _showStoreSubmissionDialog(self, ready):
        if self._publishDialog is not None:
            self._publishDialog.Raise(); self._publishDialog.checkList.SetFocus(); return
        dialog = StoreSubmissionDialog(gui.mainFrame, ready); self._publishDialog = dialog
        def close(_event=None):
            if self._publishDialog is dialog: self._publishDialog = None
            dialog.Destroy()
        def inspect(_event=None):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-ons were selected for store-submission review")); return
            close(); self._beginStorePreflight(selected)
        dialog.onUpdate = inspect; dialog.publishButton.Bind(wx.EVT_BUTTON, inspect); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _beginStorePreflight(self, ready):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Checking the selected add-ons, release packages, and current NVDA Add-on Store requirements")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._storePreflight, args=(ready,), name="addonDeveloperStorePreflight", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start store submission preflight"); ui.message(_("The store submission check could not be started"))
    def _approvedProjectResults(self, state):
        approved = engine.approved_project_ids(state); records = engine.newest_local_project_records(engine.visible_project_records(state)[0])[0]; ready = []
        for item in records:
            if not isinstance(item, dict) or item.get("project_id") not in approved: continue
            try:
                result = engine.ProjectResult(**item)
                if Path(result.path).is_file(): ready.append(result)
            except (OSError, TypeError, ValueError): pass
        return ready
    def _storePreflight(self, ready):
        callback = None; arguments = ()
        try:
            reports = publisher.preflight(ready, engine.values, self._githubProgress)
            callback, arguments = self._offerStoreSubmission, (reports,)
        except publisher.AuthenticationRequired:
            callback, arguments = self._showGitHubResult, (_("GitHub is not signed in. Publish the add-ons to GitHub before attempting store submission. Press OK to exit."),)
        except Exception as error:
            # One malformed or inaccessible project must not hide other add-ons
            # that can be verified. Retry individually and retain exact failures.
            log.exception("Combined store submission preflight failed; retrying projects individually")
            reports = []; failures = []
            for project in ready:
                try: reports.extend(publisher.preflight([project], engine.values, self._githubProgress))
                except publisher.AuthenticationRequired:
                    callback, arguments = self._showGitHubResult, (_("GitHub is not signed in. Publish the add-ons to GitHub before attempting store submission. Press OK to exit."),); break
                except Exception as projectError:
                    log.exception("Store submission inspection failed for %s", project.name)
                    failures.append(_("%s: inspection failed: %s") % (project.name, projectError))
            else: callback, arguments = self._offerStoreSubmission, (reports, failures)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerStoreSubmission(self, reports, inspectionFailures=()):
        self._rememberExistingReleases(reports); eligible = []; blocked = list(inspectionFailures)
        for report in reports:
            item = {**report.__dict__, "sourceUrl": publisher.repository_url(report.remote) if report.remote else "", "downloadUrl": report.github_release_download_url}
            reasons = publisher.store_readiness_reasons(report)
            if reasons: blocked.append(_("%s: %s") % (report.name, ", ".join(reasons)))
            else: eligible.append((report, item))
        if blocked: ui.message(_("Not ready for store submission: %s") % "; ".join(blocked))
        if not eligible:
            self._showGitHubResult(_("No add-ons are ready for store submission. Reasons: %s. Press OK to exit.") % "; ".join(blocked)); return
        names = self._limitedDetails([entry[1] for entry in eligible], lambda item: publisher.repository_name(item["sourceUrl"]))
        blockedText = _(" Add-ons not ready: %s.") % "; ".join(blocked) if blocked else ""
        message = _("Verified ready for store submission: %s.%s Select Yes to open only their prefilled submission forms. Review every field and submit each form manually; the updater never submits a store issue or pull request. Select No to exit without opening the store.") % (names, blockedText)
        def submit():
            results = [engine.ProjectResult(report.project_id, report.name, report.manifest, "GitHub release verified") for report, _item in eligible]
            urls = [publisher.store_url(item) for _report, item in eligible]
            ui.message(_("Opening prefilled store submission forms for: %s") % names); self._startSubmissionLauncher(results, urls)
        self._showConfirmation(_("Submit add-ons to the NVDA Add-on Store"), message, submit, onNo=lambda: self._showGitHubResult(_("Store submission was canceled. No store pages were opened. Press OK to exit.")))
    @scriptHandler.script(description=_("Publish updated add-ons to GitHub and optionally create releases"), gestures=("kb:NVDA+alt+g", "kb:NVDA+alt+shift+g"), category=SCRIPT_CATEGORY)
    def script_publishUpdatedAddonsToGitHub(self, gesture):
        state = engine.read_json(self._statePath, {}); ready = []; approved = engine.approved_project_ids(state); records, ignored = engine.visible_project_records(state)
        records, older = engine.newest_local_project_records(records)
        for item in records:
            if not isinstance(item, dict) or item.get("project_id") not in approved: continue
            try:
                result = engine.ProjectResult(**item)
                if Path(result.path).is_file(): ready.append(result)
            except (OSError, TypeError, ValueError): pass
        if not ready: ui.message(_("No approved add-on development projects are available to publish to GitHub")); return
        if ignored:
            saved = set(state.get("ignoredProjects", [])) if isinstance(state.get("ignoredProjects"), list) else set()
            if ignored != saved: state["ignoredProjects"] = sorted(ignored); engine.atomic_json_write(self._statePath, state)
            ui.message(_("Ignored %d duplicate Codex work copies because primary development copies are available") % len(ignored))
        if older:
            ui.message(_("Ignored %d older local add-on copies. Only the newest local version of each add-on is available for GitHub publishing.") % len(older))
        self._showGitHubPublishDialog(ready)
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
        ui.message(_("Checking GitHub authentication and add-on repository safety")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubPreflight, args=(ready,), name="addonDeveloperGitHubPreflight", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub preflight"); ui.message(_("The GitHub preflight could not be started"))
    def _monitorLoop(self):
        while not self._shutdown.is_set():
            try:
                settings = config.conf["addonDeveloperUpdater"]
                if settings["automaticChecks"]:
                    self._scanCancel.clear(); self._runCheck(manual=False, full_system=False)
                wait_seconds = max(900, int(settings["intervalMinutes"]) * 60)
            except Exception:
                log.exception("Add-on Developer Updater monitor failed")
                wait_seconds = 900
            self._wakeMonitor.wait(wait_seconds); self._wakeMonitor.clear()
    def _startCheck(self, manual=False, full_system=False):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer scan is already running")); return
        self._scanCancel.clear(); ui.message(_("Checking for NVDA releases and add-on projects"))
        try: threading.Thread(target=self._runCheck, kwargs={"manual": manual, "full_system": full_system, "lock_acquired": True}, name="addonDeveloperManualScan", daemon=True).start()
        except Exception:
            self._scanLock.release(); log.exception("Could not start add-on developer scan"); ui.message(_("The add-on developer scan could not be started"))
    def _rootStrings(self):
        settings = config.conf["addonDeveloperUpdater"]
        roots = [str(Path.home() / "Documents" / "NVDA Add-on Development" / "Add-ons")]
        for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox", "GoogleDrive", "iCloudDrive"):
            if value := os.environ.get(variable): roots.append(value)
        roots.extend(str(Path.home() / name) for name in ("OneDrive", "Dropbox", "Google Drive", "iCloudDrive"))
        roots.extend(value.strip() for value in settings["scanRoots"].split(";") if value.strip())
        return list(dict.fromkeys(os.path.normcase(os.path.expandvars(os.path.expanduser(root))) for root in roots))
    @staticmethod
    def _rescanDue(state, hours):
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["lastScanAt"])).total_seconds()
            return elapsed < -300 or elapsed >= hours * 3600
        except (KeyError, TypeError, ValueError): return True
    @classmethod
    def _workerMode(cls, manual, state, hours):
        if manual: return "manual"
        return "periodic" if cls._rescanDue(state, hours) else "background"
    @staticmethod
    def _limitedDetails(items, formatter, limit=5):
        details = [formatter(item) for item in items[:limit]]
        if len(items) > limit: details.append(_("and %d more") % (len(items) - limit))
        return "; ".join(details)
    @classmethod
    def _completionMessage(cls, results):
        if not results: return _("Scan complete. No add-on development projects were found.")
        current = [result for result in results if result.status == "current"]
        updated = [result for result in results if result.status.startswith("updated to ")]
        available = [result for result in results if result.status.startswith("update available: ")]
        awaiting = [result for result in results if "awaiting" in result.status]
        failed = [result for result in results if "failed" in result.status]
        parts = [_("Scan complete.")]
        if current: parts.append(_("1 project is current.") if len(current) == 1 else _("%d projects are current.") % len(current))
        if updated:
            details = cls._limitedDetails(updated, lambda result: _("%s to NVDA %s") % (result.name, result.status.removeprefix("updated to ")))
            parts.append(_("1 manifest updated: %s.") % details if len(updated) == 1 else _("%d manifests updated: %s.") % (len(updated), details))
        if available:
            details = cls._limitedDetails(available, lambda result: _("%s, NVDA %s") % (result.name, result.status.removeprefix("update available: ")))
            parts.append(_("1 update is available: %s. The result was saved and nothing was changed. Use the review command when you are ready.") % details if len(available) == 1 else _("%d updates are available: %s. The results were saved and nothing was changed. Use the review command when you are ready.") % (len(available), details))
        if awaiting:
            details = cls._limitedDetails(awaiting, lambda result: result.name)
            parts.append(_("1 project is awaiting approval: %s.") % details if len(awaiting) == 1 else _("%d projects are awaiting approval: %s.") % (len(awaiting), details))
        if failed:
            details = cls._limitedDetails(failed, lambda result: result.name)
            parts.append(_("1 project failed validation: %s. See the NVDA log for details.") % details if len(failed) == 1 else _("%d projects failed validation: %s. See the NVDA log for details.") % (len(failed), details))
        return " ".join(parts)
    def _showReviewDialog(self, available, release):
        if self._reviewDialog is not None:
            self._reviewDialog.Raise(); self._reviewDialog.checkList.SetFocus(); return
        dialog = UpdateReviewDialog(gui.mainFrame, available); self._reviewDialog = dialog
        def closeDialog(_event=None):
            if self._reviewDialog is dialog: self._reviewDialog = None
            dialog.Destroy()
        def updateSelected(_event):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No add-on manifests were selected")); return
            closeDialog(); self._startApply(selected, release)
        dialog.onUpdate = updateSelected
        dialog.updateButton.Bind(wx.EVT_BUTTON, updateSelected); dialog.closeButton.Bind(wx.EVT_BUTTON, closeDialog); dialog.Bind(wx.EVT_CLOSE, closeDialog)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _startApply(self, selected, release):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        self._scanCancel.clear(); ui.message(_("Updating %d selected add-on manifests") % len(selected)); self._startProgress()
        try: threading.Thread(target=self._applySelected, args=(selected, release), name="addonDeveloperApplySelected", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start selected manifest updates"); ui.message(_("The selected manifest updates could not be started"))
    def _applySelected(self, selected, release):
        cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set(); results = []
        try:
            for selectedResult in selected:
                if cancelled(): return
                path = Path(selectedResult.path)
                try: results.append(engine.update(path, release, self._backupRoot, True, cancelled))
                except Exception as error:
                    log.exception("Could not update selected add-on manifest %s", path)
                    results.append(engine.ProjectResult(selectedResult.project_id, selectedResult.name, str(path), f"validation failed: {error}"))
            state = engine.read_json(self._statePath, {}); projects = engine.merge_project_records(state.get("projects", []), results)
            updated = [result for result in results if result.status.startswith("updated to ")]
            submissionProjects = engine.merge_project_records(state.get("submissionProjects", []), updated)
            channels = state.get("submissionChannels", {}) if isinstance(state.get("submissionChannels"), dict) else {}
            for result in updated: channels[result.project_id] = "beta" if release.prerelease else "stable"
            state["projects"] = projects; state["submissionProjects"] = submissionProjects; state["submissionChannels"] = channels; state["lastScanAt"] = engine.utc_now(); engine.atomic_json_write(self._statePath, state)
            for result in results: log.info("Add-on Developer Updater selected update: %s: %s", result.path, result.status)
            if not self._shutdown.is_set(): wx.CallAfter(self._finishApply, results)
        finally: self._stopProgress(); self._scanLock.release()
    def _finishApply(self, results):
        ui.message(self._completionMessage(results))
    def _showConfirmation(self, title, message, onYes, defaultYes=False, onNo=None):
        if self._confirmDialog is not None: self._confirmDialog.Raise(); return
        dialog = ConfirmationDialog(gui.mainFrame, title, message, defaultYes=defaultYes); self._confirmDialog = dialog
        def close(_event=None):
            if self._confirmDialog is dialog: self._confirmDialog = None
            dialog.Destroy()
        def yes(_event=None): close(); onYes()
        def no(_event=None):
            close()
            if onNo is not None: onNo()
        dialog.yesButton.Bind(wx.EVT_BUTTON, yes); dialog.noButton.Bind(wx.EVT_BUTTON, no); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.defaultButton.SetFocus)
    def _showInformation(self, title, message):
        if self._informationDialog is not None: self._informationDialog.Raise(); return
        dialog = InformationDialog(gui.mainFrame, title, message); self._informationDialog = dialog
        def close(_event=None):
            if self._informationDialog is dialog: self._informationDialog = None
            dialog.Destroy()
        dialog.okButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close)
        dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.okButton.SetFocus)
    def _showGitHubResult(self, message):
        ui.message(message); self._showInformation(_("GitHub publishing complete"), message)
    def _githubPreflight(self, ready):
        callback = None; arguments = ()
        try:
            reports = publisher.preflight(ready, engine.values, self._githubProgress)
            callback, arguments = self._offerGitHubPush, (reports,)
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerGitHubLogin, (ready,)
        except Exception as error:
            log.exception("GitHub publishing preflight failed"); callback, arguments = self._showGitHubResult, (_("GitHub publishing preflight failed: %s. Press OK to exit.") % error,)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerGitHubLogin(self, ready):
        self._showConfirmation(_("Sign in to GitHub"), _("GitHub CLI is not signed in for NVDA. Sign in now? A one-time code will be copied to the clipboard and GitHub will open in your browser. Press Enter or select Yes to continue."), lambda: self._startGitHubLogin(ready), defaultYes=True, onNo=lambda: self._showGitHubResult(_("GitHub publishing was canceled. Press OK to exit.")))
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
        except Exception as error:
            log.exception("GitHub login failed"); wx.CallAfter(ui.message, _("GitHub sign-in failed: %s") % error)
        finally:
            self._stopProgress(); self._scanLock.release()
            if succeeded and not self._shutdown.is_set(): wx.CallAfter(self._beginGitHubPreflight, ready)
    def _offerGitHubPush(self, reports):
        comparisons = "; ".join(_("%s: current GitHub release %s, newest local version %s") % (report.name, getattr(report, "github_release_version", "") or _("none"), getattr(report, "version", "") or _("unknown")) for report in reports)
        if comparisons: ui.message(_("GitHub release version comparison: %s") % comparisons)
        self._rememberExistingReleases(reports)
        notOwned = [report.name for report in reports if not getattr(report, "repository_owned_by_user", True)]
        reports = [report for report in reports if getattr(report, "repository_owned_by_user", True)]
        if notOwned: ui.message(_("Skipped repositories that are not owned by the signed-in GitHub account: %s") % "; ".join(notOwned))
        reports = [report for report in reports if report.changed_files or report.unpushed_commits or not report.remote or publisher.release_needed(getattr(report, "version", ""), getattr(report, "github_release_version", ""))]
        if not reports:
            message = _("The newest local releases are already current on GitHub. Press OK to exit.")
            ui.message(message); self._showInformation(_("GitHub releases are current"), message); return
        create = [report.name for report in reports if not report.remote]; changed = sum(report.changed_files for report in reports)
        unpushed = sum(report.unpushed_commits for report in reports)
        message = _("Publish %d add-on projects to GitHub? %d changed files will be committed and %d existing local commits will be pushed. Missing public repositories will be created for: %s. Select No to make no changes.") % (len(reports), changed, unpushed, "; ".join(create) if create else _("none"))
        self._showConfirmation(_("Publish add-ons to GitHub"), message, lambda: self._startGitHubPush(reports), onNo=lambda: self._showGitHubResult(_("GitHub publishing was canceled. Press OK to exit.")))
    def _rememberExistingReleases(self, reports):
        current = [report for report in reports if getattr(report, "repository_owned_by_user", True) and not publisher.release_needed(getattr(report, "version", ""), getattr(report, "github_release_version", "")) and getattr(report, "github_release_download_url", "") and getattr(report, "remote", "")]
        if not current: return
        state = engine.read_json(self._statePath, {}); metadata = state.get("submissionMetadata", {}) if isinstance(state.get("submissionMetadata"), dict) else {}
        for report in current:
            metadata[report.project_id] = {**report.__dict__, "sourceUrl": publisher.repository_url(report.remote), "downloadUrl": report.github_release_download_url}
        state["submissionMetadata"] = metadata; engine.atomic_json_write(self._statePath, state)
    def _startGitHubPush(self, reports):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Publishing confirmed add-on projects to GitHub")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubPush, args=(reports,), name="addonDeveloperGitHubPush", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub publishing"); ui.message(_("GitHub publishing could not be started"))
    def _githubPush(self, reports):
        callback = None; arguments = ()
        try:
            publisher.validate_publish_builds(reports, self._githubProgress)
            published = publisher.push(reports, self._githubProgress); callback, arguments = self._offerGitHubRelease, (published,)
        except Exception as error:
            log.exception("GitHub publishing failed"); callback, arguments = self._showGitHubResult, (_("GitHub publishing failed: %s. Press OK to exit.") % error,)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
    def _offerGitHubRelease(self, published):
        names = "; ".join(item["name"] for item in published); ui.message(_("GitHub push completed for: %s") % names)
        differing = [item for item in published if publisher.release_needed(item.get("version", ""), item.get("github_release_version", ""))]
        if not differing:
            message = _("The newest local releases are already current on GitHub. Press OK to exit.")
            ui.message(message); self._showInformation(_("GitHub releases are current"), message); return
        comparisons = "; ".join(_("%s: GitHub %s, local %s") % (item["name"], item.get("github_release_version") or _("no release"), item.get("version") or _("unknown")) for item in differing)
        self._showConfirmation(_("Release new add-on versions"), _("The following local versions differ from the newest GitHub Releases: %s. Release the local versions now? Select No to leave only the source repositories pushed.") % comparisons, lambda: self._startGitHubRelease(differing), onNo=lambda: self._showGitHubResult(_("Source changes were pushed, but no new GitHub Release was created. Press OK to exit.")))
    def _startGitHubRelease(self, published):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Packaging and publishing GitHub Releases")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubRelease, args=(published,), name="addonDeveloperGitHubRelease", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub release publishing"); ui.message(_("GitHub release publishing could not be started"))
    def _githubRelease(self, published):
        callback = None; arguments = ()
        try:
            output = Path.home() / "Documents" / "NVDA Add-on Development" / "Packages"
            state = engine.read_json(self._statePath, {}); channels = state.get("submissionChannels", {}) if isinstance(state.get("submissionChannels"), dict) else {}
            for item in published: item["channel"] = channels.get(item["project_id"], "stable")
            metadata = state.get("submissionMetadata", {}) if isinstance(state.get("submissionMetadata"), dict) else {}
            def checkpoint(item):
                metadata[item["project_id"]] = item
                state["submissionMetadata"] = metadata
                engine.atomic_json_write(self._statePath, state)
            released = publisher.release(published, output, self._githubProgress, checkpoint)
            callback, arguments = self._showGitHubResult, (_("GitHub Releases published and store submission details saved for: %s. Press OK to exit.") % "; ".join(item["name"] for item in released),)
        except Exception as error:
            log.exception("GitHub release publishing failed"); callback, arguments = self._showGitHubResult, (_("GitHub release publishing failed: %s. Press OK to exit.") % error,)
        finally:
            self._scanLock.release()
            if callback is not None: self._finishProgressThen(callback, *arguments)
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
    def _runCheck(self, manual=False, full_system=False, lock_acquired=False):
        if not lock_acquired and not self._scanLock.acquire(blocking=False): return
        self._startProgress()
        cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set()
        requestPath = self._statePath.with_name(f"addonDeveloperUpdaterRequest-{uuid.uuid4().hex}.json")
        outputPath = self._statePath.with_name(f"addonDeveloperUpdaterResult-{uuid.uuid4().hex}.json")
        try:
            settings = config.conf["addonDeveloperUpdater"]; state = engine.read_json(self._statePath, {})
            storedPaths = [str(path) for path in engine.approved_manifest_paths(state)]
            request = {"mode": self._workerMode(manual, state, settings["periodicRescanHours"]), "includePrereleases": bool(settings["includePrereleases"]), "fullSystem": bool(full_system), "roots": self._rootStrings(), "manifestPaths": storedPaths}
            engine.atomic_json_write(requestPath, request)
            workerPath = Path(__file__).with_name("worker.ps1")
            if not workerPath.is_file(): raise RuntimeError("The external scan worker is missing. Reinstall Add-on Developer Updater.")
            command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(workerPath), "-RequestPath", str(requestPath), "-OutputPath", str(outputPath)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            self._workerProcess = subprocess.Popen(command, creationflags=flags)
            returnCode = self._workerProcess.wait(); self._workerProcess = None
            workerResult = engine.read_json(outputPath, {})
            if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
            if returnCode or not workerResult.get("ok"):
                detail = workerResult.get("error")
                if not detail: detail = f"External worker stopped before returning details (Windows status 0x{returnCode & 0xFFFFFFFF:08X})"
                raise RuntimeError(detail)
            release = engine.Release(str(workerResult["releaseTag"]), str(workerResult["manifestVersion"]), bool(workerResult.get("prerelease")), str(workerResult.get("releaseUrl", "")))
            if not manual and state.get("releaseTag") == release.tag and not self._rescanDue(state, settings["periodicRescanHours"]): return
            manifests = [Path(path) for path in workerResult.get("manifests", []) if isinstance(path, str)]
            if manual: wx.CallAfter(ui.message, _("NVDA %s was found. External project discovery completed.") % release.tag)
            approved = engine.approved_project_ids(state)
            if "approvedProjects" not in state:
                for item in state.get("projects", []) if isinstance(state.get("projects"), list) else []:
                    if not isinstance(item, dict) or not item.get("path"): continue
                    try: approved.add(engine.project_id(Path(item["path"])))
                    except (OSError, TypeError, ValueError): pass
            if manual: approved.update(engine.project_id(path) for path in manifests)
            results = []
            for path in manifests:
                if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
                identifier = engine.project_id(path)
                try:
                    if identifier not in approved:
                        metadata = engine.values(path); results.append(engine.ProjectResult(identifier, metadata.get("name", path.parent.name), str(path), "awaiting manual approval")); continue
                    results.append(engine.update(path, release, self._backupRoot, False, cancelled))
                except Exception as error:
                    log.exception("Could not process add-on manifest %s", path)
                    results.append(engine.ProjectResult(identifier, path.parent.name, str(path), f"validation failed: {error}"))
                if cancelled(): wx.CallAfter(ui.message, _("Add-on developer scan cancelled. No completion state was saved.")); return
            projects = engine.merge_project_records(state.get("projects", []), results); ignored = engine.ignored_project_ids({"projects": projects, "ignoredProjects": state.get("ignoredProjects", [])})
            visibleResults = [result for result in results if result.project_id not in ignored]
            counts = {
                "updated": sum(r.status.startswith("updated") for r in visibleResults),
                "available": sum(r.status.startswith("update available") for r in visibleResults),
                "awaiting": sum("awaiting" in r.status for r in visibleResults),
                "failed": sum("failed" in r.status for r in visibleResults),
            }
            engine.atomic_json_write(self._statePath, {"releaseTag": release.tag, "manifestVersion": release.manifest_version, "lastScanAt": engine.utc_now(), "approvedProjects": sorted(approved), "ignoredProjects": sorted(ignored), "projects": projects, "submissionProjects": state.get("submissionProjects", []), "submissionChannels": state.get("submissionChannels", {}), "submissionMetadata": state.get("submissionMetadata", {})})
            for result in results:
                if result.status != "current": log.info("Add-on Developer Updater: %s: %s", result.path, result.status)
            if manual or any(counts.values()): wx.CallAfter(ui.message, self._completionMessage(visibleResults))
        except Exception:
            log.exception("Add-on Developer Updater check failed"); wx.CallAfter(ui.message, _("The add-on developer update check failed. See the NVDA log for details."))
        finally:
            self._workerProcess = None
            for path in (requestPath, outputPath):
                try: path.unlink(missing_ok=True)
                except OSError: pass
            self._stopProgress(); self._scanLock.release()
