from __future__ import annotations

import os, subprocess, threading, uuid
from datetime import datetime, timezone
from pathlib import Path

import addonHandler, api, config, globalPluginHandler, globalVars, gui, scriptHandler, ui, wx
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
        self.instructions = wx.StaticText(self, label=_("Check the add-ons whose manifests should be updated. Nothing is selected by default. Press Space to check an item, Control+A to select all, Enter to update the checked items, or Escape to close.")); mainSizer.Add(self.instructions, 0, wx.ALL, 10)
        choices = []
        for result in results:
            if result.branch == "not a Git repository": gitState = _("manifest is not tracked by Git")
            elif not result.branch or result.branch == "Git status unavailable": gitState = _("manifest change state is unavailable")
            elif result.manifest_changed: gitState = _("manifest has uncommitted changes")
            else: gitState = _("manifest is clean")
            choices.append(_("%s: NVDA %s; minimum NVDA %s; branch %s; %s; %s") % (result.name, result.status.removeprefix("update available: "), result.minimum_version or _("unknown"), result.branch or _("unknown"), gitState, result.path))
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

class ProjectApprovalDialog(UpdateReviewDialog):
    def __init__(self, parent, results):
        super().__init__(parent, results)
        self.SetTitle(_("Approve discovered NVDA add-on projects")); self.allSelectedMessage = _("All discovered add-on projects selected")
        self.instructions.SetLabel(_("Select only development projects this add-on may monitor and offer to publishing or store workflows. Test copies, installed add-ons, forks you do not maintain, and archived copies should remain unselected. Nothing is selected by default.")); self.instructions.Wrap(850)
        labels = {"primary": _("primary development copy"), "codex": _("Codex work copy"), "other": _("other development copy")}
        self.checkList.Set([_("%s; %s; %s") % (result.name, labels[engine.path_kind(result.path)], result.path) for result in results]); self.checkList.SetName(_("Discovered add-on development projects awaiting approval")); self.updateButton.SetLabel(_("&Approve and check selected"))

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

class GitHubRepositoryUrlDialog(wx.Dialog):
    def __init__(self, parent, owner, repositories):
        super().__init__(parent, title=_("Copy an NVDA add-on link"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.repositories = repositories; self.onCopy = None; self.onCopyRepository = None; mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=_("Select a released NVDA add-on owned by %s, then choose Copy download link or Copy repository link. Press Enter to copy the download link, or Escape to close.") % owner), 0, wx.ALL, 10)
        choices = []
        for repository in repositories:
            status = _("private") if repository.private else _("public")
            if repository.archived: status += _(", archived")
            if repository.prerelease: status += _(", prerelease")
            choices.append(_("%s; release %s; file %s; %s") % (repository.full_name, repository.release_tag, repository.asset_name, status))
        self.repositoryList = wx.ListBox(self, choices=choices, style=wx.LB_SINGLE)
        if choices: self.repositoryList.SetSelection(0)
        mainSizer.Add(self.repositoryList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.VERTICAL); self.copyButton = wx.Button(self, label=_("&Copy download link")); self.copyRepositoryButton = wx.Button(self, label=_("Copy &repository link")); self.closeButton = wx.Button(self, label=_("C&lose")); self.copyButton.SetDefault()
        buttons.Add(self.copyButton, 0, wx.BOTTOM, 8); buttons.Add(self.copyRepositoryButton, 0, wx.BOTTOM, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((650, 320)); self.SetSize((850, 480)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def selectedRepository(self):
        index = self.repositoryList.GetSelection()
        return self.repositories[index] if index != wx.NOT_FOUND else None
    def _onKey(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if self.repositoryList.HasFocus() and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onCopy is not None: self.onCopy()
            return
        event.Skip()

class GitHubSingleSelectionDialog(wx.Dialog):
    def __init__(self, parent, title, instruction, items, choices, actionLabel):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.items = items; self.onAction = None; mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=instruction), 0, wx.ALL, 10)
        self.itemList = wx.ListBox(self, choices=choices, style=wx.LB_SINGLE)
        if choices: self.itemList.SetSelection(0)
        mainSizer.Add(self.itemList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.actionButton = wx.Button(self, label=actionLabel); self.closeButton = wx.Button(self, label=_("&Close")); self.actionButton.SetDefault()
        buttons.Add(self.actionButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((650, 320)); self.SetSize((850, 480)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def selectedItem(self):
        index = self.itemList.GetSelection()
        return self.items[index] if index != wx.NOT_FOUND else None
    def _onKey(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if self.itemList.HasFocus() and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onAction is not None: self.onAction()
            return
        event.Skip()

class IssueTemplateEditorDialog(wx.Dialog):
    def __init__(self, parent, repository, template, document):
        super().__init__(parent, title=_("Edit GitHub issue template"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.template = template; self.onSave = None; self.onDelete = None; self._questionIndex = -1
        self.document = document
        self.formSupported = self.document is not None and len(template.content.encode("utf-8")) <= 256 * 1024 and template.name.casefold() not in {"config.yml", "config.yaml"}
        if self.formSupported and self.document.kind == "yaml": self.formSupported = all(item.get("type") in {"input", "textarea", "dropdown", "checkboxes", "markdown"} for item in self.document.data.get("body", []))
        mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=_("Editing %s in %s on branch %s. Use the Form tab for common fields or Raw text for advanced changes. Control+S saves after confirmation; Escape closes without saving.") % (template.name, repository.full_name, repository.default_branch)), 0, wx.ALL, 10)
        self.notebook = wx.Notebook(self)
        if self.formSupported: self._createFormPage()
        rawPage = wx.Panel(self.notebook); rawSizer = wx.BoxSizer(wx.VERTICAL)
        rawSizer.Add(wx.StaticText(rawPage, label=_("Complete template text. Changes are checked before the Form tab can be reopened.")), 0, wx.ALL, 8)
        self.editor = wx.TextCtrl(rawPage, value=template.content, style=wx.TE_MULTILINE | wx.TE_RICH2 | wx.TE_DONTWRAP)
        self.editor.SetName(_("Complete issue template text"))
        rawSizer.Add(self.editor, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8); rawPage.SetSizer(rawSizer)
        self.rawPageIndex = self.notebook.GetPageCount(); self.notebook.AddPage(rawPage, _("Raw &text"), select=not self.formSupported)
        if self.formSupported: self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self._onPageChanging)
        mainSizer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.saveButton = wx.Button(self, label=_("&Save to GitHub")); self.deleteButton = wx.Button(self, label=_("&Delete template")); self.closeButton = wx.Button(self, label=_("&Close without saving")); self.saveButton.SetDefault()
        buttons.Add(self.saveButton, 0, wx.RIGHT, 8); buttons.Add(self.deleteButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((760, 540)); self.SetSize((1050, 760)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def _addLabeled(self, panel, sizer, label, control):
        labelControl = wx.StaticText(panel, label=label); labelControl.Wrap(380); control.SetName(label.replace("&", "").rstrip(":")); sizer.Add(labelControl, 0, wx.ALIGN_CENTER_VERTICAL); sizer.Add(control, 1, wx.EXPAND)
    def _createFormPage(self):
        panel = wx.Panel(self.notebook); outer = wx.BoxSizer(wx.VERTICAL)
        formHelp = wx.StaticText(panel, label=_("The form preserves unrecognized properties, but it may normalize YAML formatting and comments. Markdown means ordinary text with optional formatting symbols: start a heading with #, a bullet with -, or write a link as [link text](address). You can use normal text without any formatting. Use Raw text when exact template formatting matters.")); formHelp.Wrap(950); outer.Add(formHelp, 0, wx.EXPAND | wx.ALL, 8)
        metadata = wx.FlexGridSizer(cols=2, hgap=8, vgap=6); metadata.AddGrowableCol(1, 1)
        self.formName = wx.TextCtrl(panel); self.formDescription = wx.TextCtrl(panel); self.formTitle = wx.TextCtrl(panel); self.formLabels = wx.TextCtrl(panel); self.formAssignees = wx.TextCtrl(panel)
        self._addLabeled(panel, metadata, _("Issue template display &name, shown in GitHub's New Issue menu:"), self.formName); self._addLabeled(panel, metadata, _("Menu &description, explaining when to use this template:"), self.formDescription); self._addLabeled(panel, metadata, _("Text automatically added to the beginning of the new issue &title:"), self.formTitle); self._addLabeled(panel, metadata, _("GitHub &labels automatically applied, separated by commas:"), self.formLabels); self._addLabeled(panel, metadata, _("GitHub usernames automatically &assigned, separated by commas:"), self.formAssignees)
        outer.Add(metadata, 0, wx.EXPAND | wx.ALL, 8)
        if self.document.kind == "markdown":
            bodyLabel = wx.StaticText(panel, label=_("Instructions and questions shown to the person opening the issue. This accepts ordinary text; optional Markdown symbols can add headings, bullets, or links:")); bodyLabel.Wrap(950); outer.Add(bodyLabel, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            self.markdownBody = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_RICH2)
            self.markdownBody.SetName(_("Instructions and questions shown to the person opening the issue; enter ordinary text or optional Markdown formatting using # for headings, - for bullets, and [link text](address) for links"))
            outer.Add(self.markdownBody, 1, wx.EXPAND | wx.ALL, 8)
        else:
            questionSizer = wx.BoxSizer(wx.HORIZONTAL); left = wx.BoxSizer(wx.VERTICAL)
            left.Add(wx.StaticText(panel, label=_("Questions and information blocks, in the order shown on GitHub:")), 0, wx.BOTTOM, 4); self.questionList = wx.ListBox(panel, style=wx.LB_SINGLE); self.questionList.SetName(_("Questions and information blocks, in the order shown on GitHub")); left.Add(self.questionList, 1, wx.EXPAND)
            questionButtons = wx.BoxSizer(wx.HORIZONTAL); self.addQuestionButton = wx.Button(panel, label=_("&Add question")); self.removeQuestionButton = wx.Button(panel, label=_("&Remove selected question")); self.moveUpButton = wx.Button(panel, label=_("Move selected question &up")); self.moveDownButton = wx.Button(panel, label=_("Move selected question &down"))
            for button in (self.addQuestionButton, self.removeQuestionButton, self.moveUpButton, self.moveDownButton): questionButtons.Add(button, 0, wx.RIGHT, 5)
            left.Add(questionButtons, 0, wx.TOP, 6); questionSizer.Add(left, 1, wx.EXPAND | wx.RIGHT, 10)
            details = wx.FlexGridSizer(cols=2, hgap=8, vgap=6); details.AddGrowableCol(1, 1)
            self.questionTypeValues = ("input", "textarea", "dropdown", "checkboxes", "markdown"); self.questionTypeNames = (_("Short answer, one line"), _("Long answer, multiple lines"), _("Drop-down choice list"), _("Checkbox list"), _("Information text, no answer; ordinary text with optional formatting symbols"))
            self.questionType = wx.Choice(panel, choices=list(self.questionTypeNames)); self.questionId = wx.TextCtrl(panel); self.questionLabel = wx.TextCtrl(panel); self.questionDescription = wx.TextCtrl(panel); self.questionPlaceholder = wx.TextCtrl(panel); self.questionRendered = wx.TextCtrl(panel); self.questionValue = wx.TextCtrl(panel, style=wx.TE_MULTILINE); self.questionOptions = wx.TextCtrl(panel, style=wx.TE_MULTILINE); self.questionMultiple = wx.CheckBox(panel, label=_("Allow the person opening the issue to select more than one answer")); self.questionRequired = wx.CheckBox(panel, label=_("Require an answer before the issue can be submitted"))
            self._addLabeled(panel, details, _("Selected item &type, such as short answer, long answer, choices, checkboxes, or information text:"), self.questionType); self._addLabeled(panel, details, _("Internal question &ID used in the submitted issue, without spaces:"), self.questionId); self._addLabeled(panel, details, _("Question shown to the person opening the issue:"), self.questionLabel); self._addLabeled(panel, details, _("Additional instructions shown below the question:"), self.questionDescription); self._addLabeled(panel, details, _("Example or placeholder text shown before an answer is entered:"), self.questionPlaceholder); self._addLabeled(panel, details, _("Programming language used to format a long answer as a code block:"), self.questionRendered); self.questionValueLabel = wx.StaticText(panel, label=_("Answer filled in before the person starts typing:")); self.questionValueLabel.Wrap(380); self.questionValue.SetName(_("Answer filled in before the person starts typing")); details.Add(self.questionValueLabel, 0, wx.ALIGN_CENTER_VERTICAL); details.Add(self.questionValue, 1, wx.EXPAND); self._addLabeled(panel, details, _("Available answers, one choice per line:"), self.questionOptions); details.AddSpacer(1); details.Add(self.questionMultiple, 0); details.AddSpacer(1); details.Add(self.questionRequired, 0)
            questionSizer.Add(details, 2, wx.EXPAND); outer.Add(questionSizer, 1, wx.EXPAND | wx.ALL, 8)
            self.questionList.Bind(wx.EVT_LISTBOX, self._onQuestionSelected); self.questionType.Bind(wx.EVT_CHOICE, lambda _event: self._enableQuestionFields()); self.addQuestionButton.Bind(wx.EVT_BUTTON, self._addQuestion); self.removeQuestionButton.Bind(wx.EVT_BUTTON, self._removeQuestion); self.moveUpButton.Bind(wx.EVT_BUTTON, lambda _event: self._moveQuestion(-1)); self.moveDownButton.Bind(wx.EVT_BUTTON, lambda _event: self._moveQuestion(1))
        panel.SetSizer(outer); self.formPage = panel; self.notebook.AddPage(panel, _("&Form"), select=True); self._populateForm()
    @staticmethod
    def _listText(value):
        if isinstance(value, list): return ", ".join(str(item) for item in value)
        return str(value or "")
    @staticmethod
    def _splitList(value): return [item.strip() for item in value.split(",") if item.strip()]
    def _populateForm(self):
        data = self.document.data
        self.formName.SetValue(str(data.get("name") or "")); self.formDescription.SetValue(str(data.get("description", data.get("about", "")) or "")); self.formTitle.SetValue(str(data.get("title") or "")); self.formLabels.SetValue(self._listText(data.get("labels"))); self.formAssignees.SetValue(self._listText(data.get("assignees")))
        if self.document.kind == "markdown": self.markdownBody.SetValue(self.document.markdown_body); return
        self._questionIndex = -1; self._refreshQuestionList(0)
    def _setValue(self, data, key, value):
        if value or key in data: data[key] = value
    def _storeMetadata(self):
        data = self.document.data; self._setValue(data, "name", self.formName.GetValue().strip()); descriptionKey = "about" if self.document.kind == "markdown" else "description"; self._setValue(data, descriptionKey, self.formDescription.GetValue().strip()); self._setValue(data, "title", self.formTitle.GetValue())
        labels = self._splitList(self.formLabels.GetValue()); assignees = self._splitList(self.formAssignees.GetValue())
        self._setValue(data, "labels", ", ".join(labels) if self.document.kind == "markdown" else labels); self._setValue(data, "assignees", ", ".join(assignees) if self.document.kind == "markdown" else assignees)
        if self.document.kind == "markdown": self.document.markdown_body = self.markdownBody.GetValue()
    def _questions(self): return self.document.data.setdefault("body", [])
    def _selectedQuestionType(self):
        selection = self.questionType.GetSelection()
        return self.questionTypeValues[selection] if selection != wx.NOT_FOUND else ""
    def _questionSummary(self, item, index):
        kind = str(item.get("type") or "unknown"); typeName = self.questionTypeNames[self.questionTypeValues.index(kind)] if kind in self.questionTypeValues else kind; attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}; label = str(attributes.get("label") or attributes.get("value") or item.get("id") or "").replace("\n", " ").strip(); return _("%d. %s; %s") % (index + 1, typeName, label[:80] or _("unnamed"))
    def _refreshQuestionList(self, selection=0):
        questions = self._questions(); self.questionList.Set([self._questionSummary(item, index) for index, item in enumerate(questions)])
        if questions:
            selection = min(max(selection, 0), len(questions) - 1); self.questionList.SetSelection(selection); self._questionIndex = selection; self._loadQuestion(questions[selection])
        else: self._questionIndex = -1; self._clearQuestion()
    def _clearQuestion(self):
        self.questionType.SetSelection(wx.NOT_FOUND)
        for control in (self.questionId, self.questionLabel, self.questionDescription, self.questionPlaceholder, self.questionRendered, self.questionValue, self.questionOptions): control.SetValue(""); control.Disable()
        self.questionType.Disable(); self.questionMultiple.SetValue(False); self.questionMultiple.Disable(); self.questionRequired.SetValue(False); self.questionRequired.Disable()
    def _loadQuestion(self, item):
        self.questionType.Enable(); kind = str(item.get("type") or "input"); self.questionType.SetSelection(self.questionTypeValues.index(kind) if kind in self.questionTypeValues else 0)
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}; validations = item.get("validations") if isinstance(item.get("validations"), dict) else {}; options = attributes.get("options") if isinstance(attributes.get("options"), list) else []
        optionLabels = [str(option.get("label") or "") if isinstance(option, dict) else str(option) for option in options]
        self.questionId.SetValue(str(item.get("id") or "")); self.questionLabel.SetValue(str(attributes.get("label") or "")); self.questionDescription.SetValue(str(attributes.get("description") or "")); self.questionPlaceholder.SetValue(str(attributes.get("placeholder") or "")); self.questionRendered.SetValue(str(attributes.get("render") or "")); self.questionValue.SetValue(str(attributes.get("value") or "")); self.questionOptions.SetValue("\n".join(optionLabels)); self.questionMultiple.SetValue(bool(attributes.get("multiple"))); self.questionRequired.SetValue(bool(validations.get("required"))); self._enableQuestionFields()
    def _enableQuestionFields(self):
        kind = self._selectedQuestionType(); markdown = kind == "markdown"; options = kind in {"dropdown", "checkboxes"}
        valueLabel = _("Information shown in the issue form. Enter ordinary text, or use optional Markdown symbols: # for headings, - for bullets, and [link text](address) for links:") if markdown else _("Answer filled in before the person starts typing:"); self.questionValueLabel.SetLabel(valueLabel); self.questionValueLabel.Wrap(380); self.questionValue.SetName(valueLabel.rstrip(":"))
        self.questionId.Enable(bool(kind) and not markdown); self.questionLabel.Enable(bool(kind) and not markdown); self.questionDescription.Enable(bool(kind) and not markdown); self.questionPlaceholder.Enable(kind in {"input", "textarea"}); self.questionRendered.Enable(kind == "textarea"); self.questionValue.Enable(kind in {"input", "textarea", "markdown"}); self.questionOptions.Enable(options); self.questionMultiple.Enable(kind == "dropdown"); self.questionRequired.Enable(bool(kind) and not markdown)
    def _storeQuestion(self):
        if self._questionIndex < 0: return
        item = self._questions()[self._questionIndex]; kind = self._selectedQuestionType() or str(item.get("type") or "input"); item["type"] = kind
        if kind != "markdown": item["id"] = self.questionId.GetValue().strip()
        attributes = item.setdefault("attributes", {}); validations = item.setdefault("validations", {})
        if not isinstance(attributes, dict): attributes = {}; item["attributes"] = attributes
        if not isinstance(validations, dict): validations = {}; item["validations"] = validations
        knownAttributes = {"label", "description", "placeholder", "render", "value", "options", "multiple"}
        allowedAttributes = {
            "input": {"label", "description", "placeholder", "value"}, "textarea": {"label", "description", "placeholder", "render", "value"},
            "dropdown": {"label", "description", "options", "multiple"}, "checkboxes": {"label", "description", "options"}, "markdown": {"value"},
        }[kind]
        for key in knownAttributes - allowedAttributes: attributes.pop(key, None)
        if kind == "markdown":
            item.pop("id", None); item.pop("validations", None); attributes["value"] = self.questionValue.GetValue()
        else:
            self._setValue(attributes, "label", self.questionLabel.GetValue()); self._setValue(attributes, "description", self.questionDescription.GetValue()); self._setValue(attributes, "placeholder", self.questionPlaceholder.GetValue()); self._setValue(attributes, "render", self.questionRendered.GetValue()); self._setValue(attributes, "value", self.questionValue.GetValue()); validations["required"] = self.questionRequired.IsChecked()
            if kind in {"dropdown", "checkboxes"}:
                values = [line.strip() for line in self.questionOptions.GetValue().splitlines() if line.strip()]; old = attributes.get("options") if isinstance(attributes.get("options"), list) else []; updated = []
                for index, value in enumerate(values):
                    if kind == "checkboxes":
                        option = dict(old[index]) if index < len(old) and isinstance(old[index], dict) else {}; option["label"] = value; updated.append(option)
                    else: updated.append(value)
                attributes["options"] = updated
            if kind == "dropdown" and (self.questionMultiple.IsChecked() or "multiple" in attributes): attributes["multiple"] = self.questionMultiple.IsChecked()
    def _onQuestionSelected(self, event):
        newIndex = event.GetSelection()
        if self._questionIndex >= 0: self._storeQuestion()
        self._questionIndex = newIndex; self._loadQuestion(self._questions()[newIndex]); self.questionList.Set([self._questionSummary(item, index) for index, item in enumerate(self._questions())]); self.questionList.SetSelection(newIndex)
    def _addQuestion(self, _event):
        self._storeQuestion(); questions = self._questions(); identifier = "question_%d" % (len(questions) + 1); questions.append({"type": "textarea", "id": identifier, "attributes": {"label": _("New question"), "description": ""}, "validations": {"required": False}}); self._refreshQuestionList(len(questions) - 1); self.questionLabel.SetFocus()
    def _removeQuestion(self, _event):
        if self._questionIndex < 0: ui.message(_("No form question is selected")); return
        index = self._questionIndex; del self._questions()[index]; self._refreshQuestionList(max(0, index - 1)); ui.message(_("Question removed from the unsaved template"))
    def _moveQuestion(self, offset):
        if self._questionIndex < 0: ui.message(_("No form question is selected")); return
        self._storeQuestion(); questions = self._questions(); target = self._questionIndex + offset
        if target < 0 or target >= len(questions): ui.message(_("The question cannot be moved farther")); return
        questions[self._questionIndex], questions[target] = questions[target], questions[self._questionIndex]; self._refreshQuestionList(target); self.questionList.SetFocus()
    def _renderForm(self):
        self._storeMetadata()
        if self.document.kind == "yaml": self._storeQuestion()
        return publisher.render_issue_template_document(self.document)
    def _onPageChanging(self, event):
        if event.GetOldSelection() == 0 and event.GetSelection() == self.rawPageIndex:
            self.editor.SetValue(self._renderForm())
        elif event.GetOldSelection() == self.rawPageIndex and event.GetSelection() == 0:
            if len(self.editor.GetValue().encode("utf-8")) > 256 * 1024:
                ui.message(_("This template is too large for the form editor. Continue editing it as raw text.")); event.Veto(); return
            try: document = publisher.parse_issue_template_document(self.template, self.editor.GetValue())
            except (TypeError, ValueError) as error:
                ui.message(_("The raw template cannot be shown as a form: %s") % error); event.Veto(); return
            self.document = document; self._populateForm()
        event.Skip()
    def getContent(self):
        return self.editor.GetValue() if not self.formSupported or self.notebook.GetSelection() == self.rawPageIndex else self._renderForm()
    def initialFocus(self): return self.formName if self.formSupported else self.editor
    def _onKey(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if event.ControlDown() and key in (ord("S"), ord("s")):
            if self.onSave is not None: self.onSave()
            return
        event.Skip()

class CompatibilityTargetDialog(GitHubPublishDialog):
    def __init__(self, parent, projects, officialChoices):
        wx.Dialog.__init__(self, parent, title=_("Set an older NVDA compatibility target"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = projects; self.officialChoices = officialChoices; self.onUpdate = None; self.allSelectedMessage = _("All compatibility-target projects selected"); self._targetValues = []; mainSizer = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=_("Select add-ons first, then choose an official older NVDA version. This changes only lastTestedNVDAVersion. It does not downgrade NVDA, the add-on version, or source code, and it does not prove the code works with that NVDA release. If the source already uses newer NVDA APIs, check out the correct older source branch instead. Nothing is selected by default.")); explanation.Wrap(900); mainSizer.Add(explanation, 0, wx.ALL, 10)
        targetSizer = wx.BoxSizer(wx.HORIZONTAL); targetSizer.Add(wx.StaticText(self, label=_("Official older NVDA &target:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8); self.target = wx.Choice(self); self.target.SetName(_("Official older NVDA target shared by every selected add-on")); targetSizer.Add(self.target, 1, wx.EXPAND); mainSizer.Add(targetSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        choices = []
        for project in projects:
            if project.branch == "Git status unavailable": gitState = _("Git state unavailable")
            elif project.manifest_changed: gitState = _("manifest has uncommitted changes")
            elif project.branch == "not a Git repository": gitState = _("manifest is not tracked by Git")
            else: gitState = _("manifest is clean")
            choices.append(_("%s; current last tested NVDA %s; minimum NVDA %s; branch %s; %s; %s") % (project.name, project.current_version, project.minimum_version, project.branch, gitState, project.path))
        self.checkList = CustomCheckListBox(self, choices=choices); self.checkList.SetName(_("Add-ons whose last tested NVDA version will be changed")); mainSizer.Add(self.checkList, 1, wx.EXPAND | wx.ALL, 10)
        selectionSizer = wx.BoxSizer(wx.HORIZONTAL); selectAll = wx.Button(self, label=_("Select &all")); selectNone = wx.Button(self, label=_("Select &none")); selectAll.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(True)); selectNone.Bind(wx.EVT_BUTTON, lambda _event: self._setAll(False)); selectionSizer.Add(selectAll, 0, wx.RIGHT, 8); selectionSizer.Add(selectNone, 0); mainSizer.Add(selectionSizer, 0, wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.applyButton = wx.Button(self, label=_("&Review compatibility changes")); self.closeButton = wx.Button(self, label=_("&Close")); self.applyButton.SetDefault(); buttons.Add(self.applyButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.checkList.Bind(wx.EVT_CHECKLISTBOX, self._onChecked); self.SetSizer(mainSizer); self.SetMinSize((720, 400)); self.SetSize((980, 580)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey); self._refreshTargets()
    def _setAll(self, checked):
        super()._setAll(checked); self._refreshTargets()
    def _refreshTargets(self):
        previous = self.selectedTarget(); allowed = engine.allowed_compatibility_targets(self.selectedResults(), self.officialChoices); self._targetValues = [version for version, _experimental in allowed]
        labels = [_("%s, experimental") % version if experimental else version for version, experimental in allowed]
        self.target.Set(labels)
        if labels:
            selection = self._targetValues.index(previous) if previous in self._targetValues else 0; self.target.SetSelection(selection); self.target.Enable(); self.applyButton.Enable()
        else:
            self.target.Disable(); self.applyButton.Disable()
    def selectedTarget(self):
        selection = self.target.GetSelection()
        return self._targetValues[selection] if selection != wx.NOT_FOUND and selection < len(self._targetValues) else ""
    def _onChecked(self, event):
        self._refreshTargets(); event.Skip()
    def _onKey(self, event):
        key = event.GetKeyCode(); listFocused = event.GetEventObject() is self.checkList or self.checkList.HasFocus()
        if event.ControlDown() and key in (ord("A"), ord("a")):
            self._setAll(True); ui.message(self.allSelectedMessage); return
        if key == wx.WXK_ESCAPE: self.Close(); return
        if listFocused and key == wx.WXK_SPACE:
            index = self.checkList.GetSelection()
            if index != wx.NOT_FOUND:
                checked = not self.checkList.IsChecked(index); self.checkList.Check(index, checked); self._refreshTargets(); ui.message(_("Selected") if checked else _("Not selected"))
            return
        if listFocused and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onUpdate is not None: self.onUpdate(None)
            return
        event.Skip()

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
        super().__init__(); self._shutdown = threading.Event(); self._scanCancel = threading.Event(); self._scanActive = threading.Event(); self._wakeMonitor = threading.Event(); self._scanLock = threading.Lock(); self._progressStop = threading.Event(); self._progressThread = None; self._progressDialog = None; self._progressValue = 0; self._progressDeterminate = False; self._reviewDialog = None; self._approvalDialog = None; self._publishDialog = None; self._repositoryDialog = None; self._githubSelectionDialog = None; self._templateEditorDialog = None; self._downgradeDialog = None; self._confirmDialog = None; self._informationDialog = None; self._workerProcess = None
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
        self._stopProgress(); stopEvent = threading.Event(); self._progressStop = stopEvent; self._progressValue = 0; self._progressDeterminate = False; wx.CallAfter(self._showProgress, stopEvent, keepFocus)
        def pulse():
            while not stopEvent.wait(2) and not self._shutdown.is_set(): wx.CallAfter(self._pulseProgress, stopEvent)
        self._progressThread = threading.Thread(target=pulse, name="addonDeveloperProgress", daemon=True); self._progressThread.start()
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
            self._progressDialog.status.SetValue(_(message)); self._progressDialog.Layout()
    def _setProgressStep(self, stopEvent, message, current, total):
        if stopEvent is not self._progressStop or stopEvent.is_set(): return
        self._progressDeterminate = True
        if self._progressDialog is not None:
            self._progressValue = min(100, max(0, round(current * 100 / max(1, total)))); self._progressDialog.status.SetValue(_(message)); self._progressDialog.gauge.SetValue(self._progressValue); self._progressDialog.Layout()
    def _pulseProgress(self, stopEvent):
        if stopEvent is self._progressStop and not stopEvent.is_set() and not self._progressDeterminate and self._progressDialog is not None:
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
        if self._progressDialog is not None:
            dialog = self._progressDialog; self._progressDialog = None; dialog.Destroy()
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
        if self._approvalDialog is not None:
            self._approvalDialog.Destroy(); self._approvalDialog = None
        if self._publishDialog is not None:
            self._publishDialog.Destroy(); self._publishDialog = None
        if self._repositoryDialog is not None:
            self._repositoryDialog.Destroy(); self._repositoryDialog = None
        if self._githubSelectionDialog is not None:
            self._githubSelectionDialog.Destroy(); self._githubSelectionDialog = None
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
        state = engine.read_json(self._statePath, {}); records = engine.visible_project_records(state)[0]; approved = engine.approved_project_ids(state)
        available = sum(str(item.get("status", "")).startswith("update available: ") for item in records if isinstance(item, dict)); awaiting = sum("awaiting" in str(item.get("status", "")) for item in records if isinstance(item, dict)); failed = sum("failed" in str(item.get("status", "")) for item in records if isinstance(item, dict)); approvedCount = sum(item.get("project_id") in approved for item in records if isinstance(item, dict))
        source = str(state.get("releaseSource", "unknown")); release = str(state.get("releaseTag", "unknown")); compatibility = str(state.get("manifestVersion", "unknown"))
        message = _("Latest detected NVDA release %s, compatibility target %s, information source %s. Last successful check %s. Last complete project check %s. Last development-folder discovery %s. %d approved projects, %d updates available, %d awaiting approval, and %d validation failures.") % (release, compatibility, source, self._formatCheckTime(state.get("lastSuccessfulCheckAt")), self._formatCheckTime(state.get("lastScanAt")), self._formatCheckTime(state.get("lastDiscoveryAt")), approvedCount, available, awaiting, failed)
        if state.get("lastDiscoveryDirectories") is not None: message += _(" The last discovery visited %d directories in %s seconds%s.") % (int(state.get("lastDiscoveryDirectories", 0)), state.get("lastDiscoverySeconds", 0), _(", and stopped at its safety limit") if state.get("lastDiscoveryTruncated") else "")
        if config.conf["addonDeveloperUpdater"]["automaticChecks"]: message += _(" The next automatic check is expected in about %d minutes.") % max(1, round(self._nextAutomaticCheckDelay(state, config.conf["addonDeveloperUpdater"]) / 60))
        else: message += _(" Automatic checks are disabled.")
        if state.get("lastCheckError"): message += _(" Most recent check error: %s.") % state["lastCheckError"]
        ui.message(message); self._showInformation(_("Add-on update-check status"), message)
    @staticmethod
    def _formatCheckTime(value):
        try: return datetime.fromisoformat(str(value)).astimezone().strftime("%B %d, %Y at %I:%M %p")
        except (TypeError, ValueError): return _("never")
    @scriptHandler.script(description=_("Review newly discovered add-on projects awaiting approval"), category=SCRIPT_CATEGORY)
    def script_reviewDiscoveredAddonProjects(self, gesture):
        state = engine.read_json(self._statePath, {}); awaiting = []
        for item in engine.visible_project_records(state)[0]:
            if isinstance(item, dict) and "awaiting" in str(item.get("status", "")):
                try: awaiting.append(engine.ProjectResult(**item))
                except TypeError: pass
        if not awaiting: ui.message(_("No discovered add-on projects are awaiting approval")); return
        release = engine.Release(str(state.get("releaseTag", "")), str(state.get("manifestVersion", "")), bool(state.get("prerelease")), str(state.get("releaseUrl", "")), str(state.get("releaseSource", "saved")), str(state.get("releaseCheckedAt", "")))
        self._showProjectApprovalDialog(awaiting, release)
    @scriptHandler.script(description=_("Set an older last tested NVDA version for selected add-ons"), gesture="kb:NVDA+alt+shift+d", category=SCRIPT_CATEGORY)
    def script_downgradeAddonCompatibility(self, gesture):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Loading approved projects and official NVDA compatibility versions")); self._startProgress(keepFocus=True)
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
            else: callback, arguments = self._showInformation, (_("No compatibility targets available"), _("No approved add-on development projects are available to change."))
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
        ui.message(_("Setting %d selected manifests to last tested NVDA %s") % (len(selected), target)); self._startProgress()
        try: threading.Thread(target=self._downgradeSelected, args=(selected, target, officialVersions), name="addonDeveloperCompatibilityChange", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start compatibility change"); ui.message(_("The compatibility change could not be started"))
    def _downgradeSelected(self, selected, target, officialVersions):
        results = []
        try:
            for item in selected:
                try: results.append(engine.downgrade(Path(item.path), target, self._backupRoot, officialVersions))
                except Exception as error: results.append(engine.ProjectResult(item.project_id, item.name, item.path, f"validation failed: {error}"))
            for result in results: log.info("Add-on Developer Updater compatibility target: %s: %s", result.path, result.status)
            changed = [result for result in results if result.status.startswith("set last tested NVDA from ")]
            state = engine.read_json(self._statePath, {}); state["projects"] = engine.merge_project_records(state.get("projects", []), results)
            if changed:
                state["compatibilityUndo"] = [{"project_id": result.project_id, "name": result.name, "path": result.path, "backup_path": result.backup_path, "previous_last_tested": result.previous_last_tested, "target_last_tested": result.target_last_tested, "target_manifest_hash": result.target_manifest_hash} for result in changed]
            engine.atomic_json_write(self._statePath, state)
        finally:
            self._scanLock.release()
            self._finishProgressThen(self._finishDowngrade, results)
    def _finishDowngrade(self, results):
        changed = [item for item in results if item.status.startswith("set last tested NVDA from ")]; failed = [item for item in results if item not in changed]
        message = _("Compatibility target change complete. %d manifests changed: %s. Use the Undo most recent compatibility target changes command if these changes need to be restored.") % (len(changed), self._limitedDetails(changed, lambda item: _("%s %s") % (item.name, item.status))) if changed else _("Compatibility target change complete. No manifests were changed.")
        if failed: message += _(" %d projects were not changed: %s.") % (len(failed), "; ".join(_("%s: %s") % (item.name, item.status.removeprefix("validation failed: ")) for item in failed))
        ui.message(message); self._showInformation(_("Compatibility target results"), message)
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
        ui.message(_("Restoring manifests from the most recent compatibility update") if updateOperation else _("Restoring manifests from the most recent compatibility target operation")); self._startProgress()
        try: threading.Thread(target=self._undoCompatibilityTargets, args=(entries, stateKey, updateOperation), name="addonDeveloperCompatibilityUndo", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start compatibility undo"); ui.message(_("The compatibility changes could not be restored"))
    def _undoCompatibilityTargets(self, entries, stateKey="compatibilityUndo", updateOperation=False):
        results = []
        try:
            for entry in entries:
                try: results.append(engine.undo_compatibility_target(Path(entry["path"]), Path(entry["backup_path"]), str(entry.get("target_last_tested", "")), str(entry.get("target_manifest_hash", "")), self._backupRoot))
                except Exception as error: results.append(engine.ProjectResult(str(entry.get("project_id", "")), str(entry.get("name", "add-on")), str(entry.get("path", "")), f"undo failed: {error}"))
            restored = {os.path.normcase(result.path) for result in results if result.status.startswith("restored last tested NVDA from ")}
            state = engine.read_json(self._statePath, {}); state["projects"] = engine.merge_project_records(state.get("projects", []), results); state[stateKey] = [entry for entry in entries if os.path.normcase(str(entry.get("path", ""))) not in restored]; engine.atomic_json_write(self._statePath, state)
            for result in results: log.info("Add-on Developer Updater compatibility undo: %s: %s", result.path, result.status)
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishUndoCompatibilityTargets, results, updateOperation)
    def _finishUndoCompatibilityTargets(self, results, updateOperation=False):
        restored = [result for result in results if result.status.startswith("restored last tested NVDA from ")]; failed = [result for result in results if result not in restored]
        if restored:
            template = _("Compatibility update undo complete. %d manifests restored: %s.") if updateOperation else _("Compatibility undo complete. %d manifests restored: %s.")
            message = template % (len(restored), self._limitedDetails(restored, lambda item: _("%s %s") % (item.name, item.status)))
        else: message = _("Compatibility update undo complete. No manifests were restored.") if updateOperation else _("Compatibility undo complete. No manifests were restored.")
        if failed: message += _(" %d manifests were not restored: %s.") % (len(failed), "; ".join(_("%s: %s") % (item.name, item.status) for item in failed))
        ui.message(message); self._showInformation(_("Compatibility update undo results") if updateOperation else _("Compatibility undo results"), message)
    @scriptHandler.script(description=_("Review previously discovered NVDA add-on compatibility updates"), category=SCRIPT_CATEGORY)
    def script_reviewAddonProjectUpdates(self, gesture):
        state = engine.read_json(self._statePath, {}); available = []
        for item in engine.visible_project_records(state)[0]:
            if isinstance(item, dict) and str(item.get("status", "")).startswith("update available: "):
                try: available.append(engine.ProjectResult(**item))
                except TypeError: pass
        if not available: ui.message(_("No saved add-on compatibility updates are available")); return
        self._showReviewDialog(available, engine.Release(str(state.get("releaseTag", "")), str(state.get("manifestVersion", "")), bool(state.get("prerelease")), str(state.get("releaseUrl", "")), str(state.get("releaseSource", "saved")), str(state.get("releaseCheckedAt", ""))))
    @scriptHandler.script(description=_("Cancel the running add-on developer update scan"), category=SCRIPT_CATEGORY)
    def script_cancelAddonProjectScan(self, gesture):
        if not self._scanActive.is_set(): ui.message(_("No add-on developer scan is running")); return
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
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
        except publisher.AuthenticationRequired:
            callback, arguments = self._offerStoreGitHubLogin, (ready,)
        except Exception as error:
            # One malformed or inaccessible project must not hide other add-ons
            # that can be verified. Retry individually and retain exact failures.
            log.exception("Combined store submission preflight failed; retrying projects individually")
            reports = []; failures = []
            for project in ready:
                try: reports.extend(publisher.preflight([project], engine.values, self._githubProgress))
                except publisher.GitHubCliRequired:
                    callback, arguments = self._offerGitHubCliInstall, (); break
                except publisher.AuthenticationRequired:
                    callback, arguments = self._offerStoreGitHubLogin, (ready,); break
                except Exception as projectError:
                    log.exception("Store submission inspection failed for %s", project.name)
                    failures.append(_("%s: inspection failed: %s") % (project.name, projectError))
            else: callback, arguments = self._offerStoreSubmission, (reports, failures)
        finally:
            self._scanLock.release()
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
            self._githubSelectionDialog.Raise(); self._githubSelectionDialog.itemList.SetFocus(); return
        if self._templateEditorDialog is not None:
            self._templateEditorDialog.Raise(); self._templateEditorDialog.initialFocus().SetFocus(); return
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
        dialog = GitHubSingleSelectionDialog(gui.mainFrame, title, instruction, repositories, choices, actionLabel); self._githubSelectionDialog = dialog
        def close(_event=None):
            if self._githubSelectionDialog is dialog: self._githubSelectionDialog = None
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
        dialog = GitHubSingleSelectionDialog(gui.mainFrame, _("Choose a GitHub issue template"), _("Select an issue template from %s and press Enter to edit it.") % repository.full_name, templates, choices, _("&Edit template")); self._githubSelectionDialog = dialog
        def close(_event=None):
            if self._githubSelectionDialog is dialog: self._githubSelectionDialog = None
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
        ui.message(message); self._showInformation(_("Issue template saved") if succeeded else _("Issue-template save failed"), message)
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
        ui.message(message); self._showInformation(_("Issue template deleted") if succeeded else _("Issue-template deletion failed"), message)
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
        ui.message(_("Checking GitHub authentication and add-on repository safety")); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._githubPreflight, args=(ready,), name="addonDeveloperGitHubPreflight", daemon=True).start()
        except Exception:
            self._stopProgress(); self._scanLock.release(); log.exception("Could not start GitHub preflight"); ui.message(_("The GitHub preflight could not be started"))
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
    def _startCheck(self, manual=False, full_system=False):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer scan is already running")); return
        self._scanCancel.clear(); self._scanActive.set(); ui.message(_("Checking for NVDA releases and add-on projects"))
        try: threading.Thread(target=self._runCheck, kwargs={"manual": manual, "full_system": full_system, "lock_acquired": True}, name="addonDeveloperManualScan", daemon=True).start()
        except Exception:
            self._scanActive.clear(); self._scanLock.release(); log.exception("Could not start add-on developer scan"); ui.message(_("The add-on developer scan could not be started"))
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
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state.get("lastDiscoveryAt") or state["lastScanAt"])).total_seconds()
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
        deferred = [result for result in results if "deferred" in result.status]
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
            parts.append(_("1 project could not be processed: %s. See the NVDA log for details.") % details if len(failed) == 1 else _("%d projects could not be processed: %s. See the NVDA log for details.") % (len(failed), details))
        if deferred:
            details = cls._limitedDetails(deferred, lambda result: _("%s: %s") % (result.name, result.status))
            parts.append(_("The update was deferred: %s.") % details)
        return " ".join(parts)
    @staticmethod
    def _addProjectContext(result, path):
        try:
            metadata = engine.values(path); result.minimum_version = metadata.get("minimumnvdaversion", ""); result.branch, result.manifest_changed = engine.git_manifest_state(path)
        except (OSError, UnicodeError, ValueError):
            pass
        return result
    def _showProjectApprovalDialog(self, awaiting, release):
        if self._approvalDialog is not None: self._approvalDialog.Raise(); self._approvalDialog.checkList.SetFocus(); return
        dialog = ProjectApprovalDialog(gui.mainFrame, awaiting); self._approvalDialog = dialog
        def close(_event=None):
            if self._approvalDialog is dialog: self._approvalDialog = None
            dialog.Destroy()
        def approve(_event=None):
            selected = dialog.selectedResults()
            if not selected: ui.message(_("No discovered add-on projects were selected")); return
            details = "; ".join(_("%s; %s") % (item.name, item.path) for item in selected)
            self._showConfirmation(_("Confirm approved add-on projects"), _("Approve %d development projects for future monitoring, GitHub publishing, and store-readiness workflows? Confirm that these are copies you maintain. Projects: %s.") % (len(selected), details), lambda: (close(), self._startApproveProjects(selected, release)))
        dialog.onUpdate = approve; dialog.updateButton.Bind(wx.EVT_BUTTON, approve); dialog.closeButton.Bind(wx.EVT_BUTTON, close); dialog.Bind(wx.EVT_CLOSE, close); dialog.Show(); dialog.Raise(); wx.CallAfter(dialog.checkList.SetFocus)
    def _startApproveProjects(self, selected, release):
        if not self._scanLock.acquire(blocking=False): ui.message(_("An add-on developer operation is already running")); return
        ui.message(_("Validating %d selected projects before approval") % len(selected)); self._startProgress(keepFocus=True)
        try: threading.Thread(target=self._approveProjects, args=(selected, release, self._progressStop), name="addonDeveloperProjectApproval", daemon=True).start()
        except Exception: self._stopProgress(); self._scanLock.release(); log.exception("Could not start project approval"); ui.message(_("Project approval could not be started"))
    def _approveProjects(self, selected, release, progressEvent):
        results = []; operationError = ""
        try:
            state = engine.read_json(self._statePath, {}); approved = engine.approved_project_ids(state)
            latest = engine.latest_release(bool(config.conf["addonDeveloperUpdater"]["includePrereleases"]), self._releaseCachePath)
            if engine.version_tuple(latest.manifest_version) >= engine.version_tuple(release.manifest_version): release = latest
            for index, item in enumerate(selected, 1):
                path = Path(item.path)
                wx.CallAfter(self._setProgressStep, progressEvent, _("Validating project %d of %d: %s") % (index, len(selected), item.name), index, len(selected))
                try: result = self._addProjectContext(engine.update(path, release, self._backupRoot, False), path)
                except Exception as error: result = engine.ProjectResult(item.project_id, item.name, item.path, f"validation failed: {error}")
                if "failed" in result.status: result.status = "awaiting manual approval; " + result.status
                else: approved.add(result.project_id)
                results.append(result)
            state["approvedProjects"] = sorted(approved); state["projects"] = engine.merge_project_records(state.get("projects", []), results); engine.atomic_json_write(self._statePath, state)
        except Exception as error:
            operationError = str(error); log.exception("Project approval failed")
        finally:
            self._scanLock.release(); self._finishProgressThen(self._finishProjectApproval, results, release, operationError)
    def _finishProjectApproval(self, results, release, operationError=""):
        if operationError:
            message = _("Project approval failed before its results could be saved: %s. No project should be treated as newly approved.") % operationError; ui.message(message); self._showInformation(_("Project approval failed"), message); return
        approved = [item for item in results if "awaiting" not in item.status]; failed = [item for item in results if item not in approved]; available = [item for item in approved if item.status.startswith("update available: ")]
        message = _("Project approval complete. %d projects approved: %s.") % (len(approved), self._limitedDetails(approved, lambda item: item.name)) if approved else _("Project approval complete. No projects were approved.")
        if failed: message += _(" %d projects remain unapproved because validation failed: %s.") % (len(failed), self._limitedDetails(failed, lambda item: item.name))
        ui.message(message)
        if available: self._showReviewDialog(available, release)
        else: self._showInformation(_("Project approval results"), message)
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
            details = "; ".join(_("%s: %s; branch %s; %s") % (item.name, item.status.removeprefix("update available: "), item.branch or _("unknown"), item.path) for item in selected)
            dirty = [item.name for item in selected if item.manifest_changed]
            message = _("Update only lastTestedNVDAVersion for %d selected manifests? The latest official NVDA target will be rechecked before writing. Planned changes: %s.") % (len(selected), details)
            if dirty: message += _(" Warning: these manifests have uncommitted changes: %s. Current contents will be backed up before editing.") % "; ".join(dirty)
            self._showConfirmation(_("Confirm add-on compatibility updates"), message, lambda: (closeDialog(), self._startApply(selected, release)))
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
            latest = engine.latest_release(bool(config.conf["addonDeveloperUpdater"]["includePrereleases"]), self._releaseCachePath)
            if engine.version_tuple(latest.manifest_version) > engine.version_tuple(release.manifest_version):
                results = [engine.ProjectResult(item.project_id, item.name, item.path, _("update deferred because newer NVDA %s is now available; run the update check again") % latest.tag) for item in selected]
                if not self._shutdown.is_set(): wx.CallAfter(self._finishApply, results)
                return
            if engine.version_tuple(latest.manifest_version) == engine.version_tuple(release.manifest_version): release = latest
            for selectedResult in selected:
                if cancelled(): return
                path = Path(selectedResult.path)
                try: results.append(self._addProjectContext(engine.update(path, release, self._backupRoot, True, cancelled), path))
                except Exception as error:
                    log.exception("Could not update selected add-on manifest %s", path)
                    results.append(engine.ProjectResult(selectedResult.project_id, selectedResult.name, str(path), f"validation failed: {error}"))
            state = engine.read_json(self._statePath, {}); projects = engine.merge_project_records(state.get("projects", []), results)
            updated = [result for result in results if result.status.startswith("updated to ")]
            submissionProjects = engine.merge_project_records(state.get("submissionProjects", []), updated)
            channels = state.get("submissionChannels", {}) if isinstance(state.get("submissionChannels"), dict) else {}
            for result in updated: channels[result.project_id] = "beta" if release.prerelease else "stable"
            state["projects"] = projects; state["submissionProjects"] = submissionProjects; state["submissionChannels"] = channels; state["lastScanAt"] = engine.utc_now()
            if updated: state["updateUndo"] = [{"project_id": result.project_id, "name": result.name, "path": result.path, "backup_path": result.backup_path, "previous_last_tested": result.previous_last_tested, "target_last_tested": result.target_last_tested, "target_manifest_hash": result.target_manifest_hash} for result in updated]
            engine.atomic_json_write(self._statePath, state)
            for result in results: log.info("Add-on Developer Updater selected update: %s: %s", result.path, result.status)
            if not self._shutdown.is_set(): wx.CallAfter(self._finishApply, results)
        except Exception as error:
            log.exception("Selected manifest update operation failed")
            results = [engine.ProjectResult(item.project_id, item.name, item.path, _("update operation failed while saving complete results; inspect the manifest and its backup: %s") % error) for item in selected]
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
        except publisher.GitHubCliRequired:
            callback, arguments = self._offerGitHubCliInstall, ()
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
        blocked = [item for item in published if item.get("releaseBlocker")]
        if blocked:
            ui.message(_("GitHub release deferred for: %s") % "; ".join(_("%s: %s") % (item["name"], item["releaseBlocker"]) for item in blocked))
        differing = [item for item in published if not item.get("releaseBlocker") and publisher.release_needed(item.get("version", ""), item.get("github_release_version", ""))]
        if not differing:
            if blocked:
                message = _("Source changes were pushed, but no release was created. Release blockers: %s. Press OK to exit.") % "; ".join(_("%s: %s") % (item["name"], item["releaseBlocker"]) for item in blocked)
                ui.message(message); self._showInformation(_("GitHub release deferred"), message); return
            message = _("The newest local releases are already current on GitHub. Press OK to exit.")
            ui.message(message); self._showInformation(_("GitHub releases are current"), message); return
        comparisons = "; ".join(_("%s: GitHub %s, local %s") % (item["name"], item.get("github_release_version") or _("no release"), item.get("version") or _("unknown")) for item in differing)
        blockedText = _(" Releases deferred until their commits reach the default branch: %s.") % "; ".join(item["name"] for item in blocked) if blocked else ""
        self._showConfirmation(_("Release new add-on versions"), _("The following local versions differ from the newest GitHub Releases: %s.%s Release the eligible local versions now? Select No to leave only the source repositories pushed.") % (comparisons, blockedText), lambda: self._startGitHubRelease(differing), onNo=lambda: self._showGitHubResult(_("Source changes were pushed, but no new GitHub Release was created. Press OK to exit.")))
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
    def _finishManualCheck(self, results, awaiting, release, sameCompatibilityFamily, discoveryTruncated=False):
        message = self._completionMessage(results)
        if sameCompatibilityFamily:
            message = _("NVDA %s was found, but it uses the same add-on compatibility target %s, so no manifest change is required solely for this build. ") % (release.tag, release.manifest_version) + message
        if release.source == "cached":
            message = _("The live NVDA release services were unavailable, so this scan used the last known release information. ") + message
        if discoveryTruncated: message += _(" Discovery stopped at its directory or time safety limit. Add narrower development folders in settings if a project was missed.")
        ui.message(message)
        if awaiting: self._showProjectApprovalDialog(awaiting, release)
    @staticmethod
    def _checkErrorText(error):
        detail = " ".join(str(error).split())[:300] or error.__class__.__name__
        lowered = detail.casefold()
        if any(word in lowered for word in ("url", "http", "network", "timed out", "name resolution", "connection")):
            return _("NVDA release services could not be reached: %s") % detail
        if "worker" in lowered or "powershell" in lowered:
            return _("The isolated project-discovery worker failed: %s") % detail
        return _("The update check failed: %s") % detail
    def _runCheck(self, manual=False, full_system=False, lock_acquired=False):
        if not lock_acquired and not self._scanLock.acquire(blocking=False): return
        self._scanActive.set(); progressStarted = False; progressEvent = None; finishCallback = None
        cancelled = lambda: self._scanCancel.is_set() or self._shutdown.is_set()
        requestPath = self._statePath.with_name(f"addonDeveloperUpdaterRequest-{uuid.uuid4().hex}.json")
        outputPath = self._statePath.with_name(f"addonDeveloperUpdaterResult-{uuid.uuid4().hex}.json")
        try:
            settings = config.conf["addonDeveloperUpdater"]; state = engine.read_json(self._statePath, {})
            mode = self._workerMode(manual, state, settings["periodicRescanHours"])
            if manual or mode == "periodic":
                self._startProgress(); progressStarted = True; progressEvent = self._progressStop; wx.CallAfter(self._setProgressMessage, _("Checking official NVDA release information"))
            release = engine.latest_release(bool(settings["includePrereleases"]), self._releaseCachePath)
            if progressStarted: wx.CallAfter(self._setProgressMessage, _("Discovering add-on development projects in the isolated worker"))
            storedPaths = [str(path) for path in engine.approved_manifest_paths(state)]
            checkedAt = engine.utc_now(); previousAutomaticError = str(state.get("lastAutomaticError", "")); previousReleaseSource = str(state.get("releaseSource", ""))
            if not manual and state.get("releaseTag") == release.tag and not self._rescanDue(state, settings["periodicRescanHours"]):
                state.update({"lastCheckAt": checkedAt, "lastSuccessfulCheckAt": checkedAt, "lastCheckError": "", "lastAutomaticError": "", "automaticFailureCount": 0, "releaseSource": release.source, "releaseCheckedAt": release.checked_at, "prerelease": release.prerelease, "releaseUrl": release.url})
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
            discoveryTruncated = bool(workerResult.get("truncated")); previousDiscoveryTruncated = bool(state.get("lastDiscoveryTruncated"))
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
                        metadata = engine.values(path); results.append(engine.ProjectResult(identifier, metadata.get("name", path.parent.name), str(path), "awaiting manual approval")); continue
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
            state.update({"releaseTag": release.tag, "manifestVersion": release.manifest_version, "prerelease": release.prerelease, "releaseUrl": release.url, "releaseSource": release.source, "releaseCheckedAt": release.checked_at, "lastCheckAt": checkedAt, "lastSuccessfulCheckAt": checkedAt, "lastScanAt": checkedAt, "lastCheckError": "", "lastAutomaticError": "", "automaticFailureCount": 0, "approvedProjects": sorted(approved), "ignoredProjects": sorted(ignored), "projects": projects, "notifiedUpdates": currentUpdates, "notifiedAwaiting": currentAwaiting, "notifiedFailures": currentFailures})
            if mode != "background": state.update({"lastDiscoveryAt": checkedAt, "lastDiscoveryDirectories": int(workerResult.get("directoriesVisited", 0)), "lastDiscoverySeconds": workerResult.get("elapsedSeconds", 0), "lastDiscoveryTruncated": discoveryTruncated})
            engine.atomic_json_write(self._statePath, state)
            for result in results:
                if result.status != "current": log.info("Add-on Developer Updater: %s: %s", result.path, result.status)
            awaiting = [result for result in visibleResults if "awaiting" in result.status]
            if manual: finishCallback = (self._finishManualCheck, visibleResults, awaiting, release, sameCompatibilityFamily, discoveryTruncated)
            else:
                if newlyActionable: wx.CallAfter(ui.message, self._completionMessage(newlyActionable))
                if resolvedFailures: wx.CallAfter(ui.message, _("Add-on validation is working again for: %s") % "; ".join(resolvedFailures))
                if previousAutomaticError: wx.CallAfter(ui.message, _("Automatic NVDA add-on update checks are working again"))
                if release.source == "cached" and previousReleaseSource != "cached": wx.CallAfter(ui.message, _("Live NVDA release services are unavailable. Automatic checks are using the last known release information."))
                elif release.source != "cached" and previousReleaseSource == "cached": wx.CallAfter(ui.message, _("Live NVDA release information is available again"))
                if discoveryTruncated and not previousDiscoveryTruncated: wx.CallAfter(ui.message, _("Periodic add-on discovery stopped at its safety limit. Add narrower development folders in settings if a project was missed."))
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
