"""Accessible wx dialogs used by Add-on Developer Updater."""

from __future__ import annotations

import ctypes

import addonHandler, ui, wx
from gui.nvdaControls import CustomCheckListBox
from . import engine, publisher

addonHandler.initTranslation()

def _projectDisplayName(result):
    """Return public metadata prepared by a worker without touching the filesystem."""
    return str(getattr(result, "public_name", "") or getattr(result, "summary", "") or result.name)

class UpdateReviewDialog(wx.Dialog):
    def __init__(self, parent, results, release=None, storeNote=""):
        super().__init__(parent, title=_("Review NVDA add-on compatibility updates"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = results; mainSizer = wx.BoxSizer(wx.VERTICAL)
        self.onUpdate = None; self.allSelectedMessage = _("All add-on updates selected")
        instruction = _("Check the add-ons whose manifests should be updated. Nothing is selected by default. Press Space to check an item, Control+A to select all, Enter to update the checked items, or Escape to close.")
        if release is not None:
            build = release.tag + (f" ({release.build_version})" if release.build_version and release.build_version != release.tag else "")
            instruction = _("Selected NVDA build: %s. Manifest compatibility target: %s. ") % (build, release.manifest_version) + instruction
            if release.prerelease: instruction += " " + _("This NVDA build is a test release, so try your add-ons with it before publishing.")
        if storeNote: instruction += " " + storeNote
        self.instructions = wx.StaticText(self, label=instruction); self.instructions.Wrap(900); mainSizer.Add(self.instructions, 0, wx.ALL, 10)
        choices = []
        # Public names and versions tell copies of one add-on apart; internal IDs can be shared.
        versions = {}
        for result in results: versions.setdefault((result.summary or result.name).casefold(), []).append(result.addon_version)
        for result in results:
            if result.branch == "not a Git repository": gitState = _("manifest is not tracked by Git")
            elif not result.branch or result.branch == "Git status unavailable": gitState = _("manifest change state is unavailable")
            elif result.manifest_changed: gitState = _("manifest has uncommitted changes")
            else: gitState = _("manifest is clean")
            rank = engine.copy_rank(result.addon_version, versions[(result.summary or result.name).casefold()], _)
            label = f"{result.summary or result.name} {result.addon_version}".strip() + (f" ({rank})" if rank else "")
            choices.append(_("%s: NVDA %s; minimum NVDA %s; branch %s; %s; %s") % (label, result.status.removeprefix("update available: "), result.minimum_version or _("unknown"), result.branch or _("unknown"), gitState, result.path))
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
    def __init__(self, parent, results, trackedVersions=None):
        # Copies of the same add-on sit together, newest first, so differences are easy to compare.
        publicName = lambda result: (result.summary or result.name).casefold()
        results = sorted(results, key=lambda result: (publicName(result), tuple(-number for number in engine.addon_version_key(result.addon_version)[0]), result.path.casefold()))
        super().__init__(parent, results)
        trackedVersions = trackedVersions or {}
        versions = {}
        for result in results: versions.setdefault(publicName(result), []).append(result.addon_version)
        # Plain wording: the choice is simply which of these add-ons the user is developing.
        self.SetTitle(_("Choose add-ons to track")); self.allSelectedMessage = _("All add-ons checked")
        self.instructions.SetLabel(_("These add-on projects were found on your computer. Check each one you are developing yourself. Tracked add-ons are checked for new NVDA versions and can be published to GitHub and submitted to the Add-on Store. Leave an add-on unchecked if you are not developing it, for example someone else's add-on you downloaded, a backup, or a test copy. Each line tells you the add-on's name and version, whether it is the newest copy found, whether you already track another copy, and whether its code is on GitHub or only on this computer. Nothing is checked yet. Space checks an add-on, and Enter continues. To change your choices later, use Manage tracked add-ons, NVDA+Alt+Shift+A.")); self.instructions.Wrap(850)
        self.checkList.Set([engine.describe_copy(result, versions[publicName(result)], trackedVersions.get(publicName(result), []), translate=_) for result in results]); self.checkList.SetName(_("Add-on projects found on this computer")); self.updateButton.SetLabel(_("&Track checked add-ons"))
        # Without this, every unchecked copy is offered again after each scan.
        self.hideUnchecked = wx.CheckBox(self, label=_("&Hide the unchecked add-ons so they are not offered again"))
        self.GetSizer().Insert(2, self.hideUnchecked, 0, wx.ALL, 10); self.hideUnchecked.MoveAfterInTabOrder(self.checkList); self.Layout()
    def uncheckedResults(self):
        return [result for index, result in enumerate(self.results) if not self.checkList.IsChecked(index)]

class GitHubPublishDialog(UpdateReviewDialog):
    def __init__(self, parent, results):
        wx.Dialog.__init__(self, parent, title=_("Select add-ons to publish to GitHub"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.results = results; self.onUpdate = None; self.allSelectedMessage = _("All GitHub projects selected"); mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=_("Check the add-on projects to inspect and publish. Nothing is selected by default. Press Space to check an item, Control+A to select all, Enter to continue, or Escape to close.")), 0, wx.ALL, 10)
        labels = {"primary": _("primary development copy"), "codex": _("Codex work copy"), "other": _("development copy")}
        self.checkList = CustomCheckListBox(self, choices=[_("%s; %s; %s") % (_projectDisplayName(result), labels[engine.path_kind(result.path)], result.path) for result in results])
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
    def __init__(self, parent, title, instruction, items, choices, actionLabel, extraActionLabel=None):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.items = items; self.onAction = None; self.extraActionLabel = extraActionLabel; mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(wx.StaticText(self, label=instruction), 0, wx.ALL, 10)
        self.itemList = wx.ListBox(self, choices=choices, style=wx.LB_SINGLE)
        if choices: self.itemList.SetSelection(0)
        mainSizer.Add(self.itemList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.actionButton = wx.Button(self, label=actionLabel); self.closeButton = wx.Button(self, label=_("&Close")); self.actionButton.SetDefault()
        buttons.Add(self.actionButton, 0, wx.RIGHT, 8)
        # A second action for the same selection, such as reviewing pull requests next to opening Issues.
        self.extraButton = wx.Button(self, label=extraActionLabel) if extraActionLabel else None
        if self.extraButton is not None: buttons.Add(self.extraButton, 0, wx.RIGHT, 8)
        if not items: self.actionButton.Disable()
        buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
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
        self.formSupported = publisher.issue_template_form_supported(template, self.document)
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
        if not template.sha: self.deleteButton.Disable()  # A new template does not exist on GitHub yet.
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
            if not publisher.issue_template_form_supported(self.template, document, self.editor.GetValue()):
                ui.message(_("This template contains a question type that the accessible form cannot safely edit. Continue editing it as raw text.")); event.Veto(); return
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

class ScanTargetDialog(wx.Dialog):
    def __init__(self, parent, releases, preferredTag=""):
        super().__init__(parent, title=_("Choose the NVDA compatibility scan target"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.releases = releases; self.onStart = None; mainSizer = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=_("Choose the exact NVDA build to check. Stable, beta, release candidate, and alpha are separate choices. The full build identifies what will be tested; the manifest compatibility target is the value proposed for lastTestedNVDAVersion. Scans propose only newer targets and make no manifest changes. Use the compatibility downgrade command when an existing manifest must be set to an older target.")); explanation.Wrap(850); mainSizer.Add(explanation, 0, wx.ALL, 10)
        channelNames = {"stable": _("stable"), "beta": _("beta"), "rc": _("release candidate"), "alpha": _("alpha"), "prerelease": _("prerelease")}
        labels = []
        for release in releases:
            build = release.tag + (f" ({release.build_version})" if release.build_version and release.build_version != release.tag else "")
            labels.append(_("%s; NVDA build %s; manifest compatibility target %s; information source %s") % (channelNames.get(engine.release_channel(release), engine.release_channel(release)), build, release.manifest_version, release.source))
        self.targetList = wx.ListBox(self, choices=labels, style=wx.LB_SINGLE); self.targetList.SetName(_("Available NVDA builds and manifest compatibility targets"))
        selection = next((index for index, release in enumerate(releases) if release.tag.casefold() == preferredTag.casefold()), wx.NOT_FOUND)
        if selection == wx.NOT_FOUND: selection = next((index for index, release in enumerate(releases) if engine.release_channel(release) == "stable"), 0)
        if labels: self.targetList.SetSelection(selection)
        mainSizer.Add(self.targetList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.startButton = wx.Button(self, label=_("&Start scan with selected build")); self.closeButton = wx.Button(self, label=_("&Close")); self.startButton.SetDefault(); buttons.Add(self.startButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); mainSizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(mainSizer); self.SetMinSize((700, 340)); self.SetSize((900, 480)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def selectedRelease(self):
        index = self.targetList.GetSelection()
        return self.releases[index] if index != wx.NOT_FOUND else None
    def _onKey(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if self.targetList.HasFocus() and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onStart is not None: self.onStart()
            return
        event.Skip()

class ManageTrackingDialog(wx.Dialog):
    """Change whether each found copy is tracked, not tracked, or hidden.

    Each list line starts with the current choice, so speech and braille state it
    before the description.  Letters change the choice without leaving the list.
    """
    STATE_ORDER = ("tracked", "untracked", "hidden")
    def __init__(self, parent, entries):
        super().__init__(parent, title=_("Manage tracked add-ons"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.entries = entries; self.original = {result.project_id: state for result, state, _description in entries}; self.choices = dict(self.original); self.onSave = None
        self.stateNames = {"tracked": _("Tracked"), "untracked": _("Not tracked"), "hidden": _("Hidden")}
        sizer = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.StaticText(self, label=_("Every add-on copy found on your computer is listed with your current choice first. Tracked add-ons are checked for new NVDA versions and can be published to GitHub and the Add-on Store. Not tracked add-ons are offered again after each scan. Hidden add-ons are not offered again until you change them here. Select an add-on, then press T for tracked, N for not tracked, or H for hidden. Press Enter or the Save button to apply your changes."))
        instructions.Wrap(900); sizer.Add(instructions, 0, wx.ALL, 10)
        self.copyList = wx.ListBox(self, choices=[self._label(index) for index in range(len(entries))], style=wx.LB_SINGLE); self.copyList.SetName(_("Add-on copies found on this computer"))
        if entries: self.copyList.SetSelection(0)
        sizer.Add(self.copyList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        row = wx.BoxSizer(wx.HORIZONTAL); row.Add(wx.StaticText(self, label=_("&Choice for the selected add-on:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.stateChoice = wx.Choice(self, choices=[self.stateNames[state] for state in self.STATE_ORDER]); self.stateChoice.SetName(_("Choice for the selected add-on")); row.Add(self.stateChoice, 0); sizer.Add(row, 0, wx.ALL, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.saveButton = wx.Button(self, label=_("&Save changes")); self.closeButton = wx.Button(self, label=_("C&lose without saving")); self.saveButton.SetDefault()
        buttons.Add(self.saveButton, 0, wx.RIGHT, 8); buttons.Add(self.closeButton, 0); sizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(sizer); self.SetMinSize((700, 400)); self.SetSize((1000, 560)); self.CentreOnScreen()
        self.copyList.Bind(wx.EVT_LISTBOX, lambda _event: self._syncChoice()); self.stateChoice.Bind(wx.EVT_CHOICE, self._onChoice); self.Bind(wx.EVT_CHAR_HOOK, self._onKey); self._syncChoice()
    def _label(self, index):
        result, _state, description = self.entries[index]
        return _("%s: %s") % (self.stateNames[self.choices[result.project_id]], description)
    def _syncChoice(self):
        index = self.copyList.GetSelection()
        if index != wx.NOT_FOUND: self.stateChoice.SetSelection(self.STATE_ORDER.index(self.choices[self.entries[index][0].project_id]))
    def _setState(self, index, state, announce=True):
        result = self.entries[index][0]
        if self.choices[result.project_id] != state:
            self.choices[result.project_id] = state; self.copyList.SetString(index, self._label(index)); self.copyList.SetSelection(index)
        self._syncChoice()
        if announce: ui.message(self.stateNames[state])
    def _onChoice(self, _event):
        index = self.copyList.GetSelection(); selection = self.stateChoice.GetSelection()
        if index != wx.NOT_FOUND and selection != wx.NOT_FOUND: self._setState(index, self.STATE_ORDER[selection], announce=False)
    def changes(self):
        return {identifier: state for identifier, state in self.choices.items() if state != self.original[identifier]}
    def _onKey(self, event):
        key = event.GetKeyCode(); listFocused = self.copyList.HasFocus()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if listFocused and not event.HasModifiers() and key in (ord("T"), ord("t"), ord("N"), ord("n"), ord("H"), ord("h")):
            index = self.copyList.GetSelection()
            if index != wx.NOT_FOUND: self._setState(index, {"t": "tracked", "n": "untracked", "h": "hidden"}[chr(key).lower()])
            return
        if listFocused and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onSave is not None: self.onSave()
            return
        event.Skip()

def pullRequestLabel(pull):
    """Describe a pull request in one line, most important facts first."""
    checks = {"passed": _("checks passed"), "failed": _("checks failed"), "running": _("checks still running"), "none": _("no automatic checks")}[pull.checks]
    merge = {"MERGEABLE": _("can be merged"), "CONFLICTING": _("has conflicts that must be resolved first")}.get(pull.mergeable, _("GitHub is still checking whether it can be merged"))
    origin = _(", from their own copy of the repository") if pull.from_fork else ""
    draft = _("draft; ") if pull.draft else ""
    return _("#%d %s, by %s%s; %s%d files changed, %d lines added, %d removed; %s; %s; branch %s into %s") % (pull.number, pull.title, pull.author, origin, draft, pull.changed_files, pull.additions, pull.deletions, checks, merge, pull.head_branch, pull.base_branch)

class PullRequestDialog(wx.Dialog):
    """List a repository's open pull requests and offer to open or merge the selected one."""
    def __init__(self, parent, repository, pulls):
        super().__init__(parent, title=_("Pull requests for %s") % repository.full_name, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.pulls = pulls; self.onOpen = None; self.onMerge = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.StaticText(self, label=_("Open pull requests, oldest first. Each line gives the title, author, size, whether the automatic checks passed, and whether it can be merged. Open it in your browser to read the changes before merging. Merging adds the changes to the repository on GitHub; your own copy on this computer gets them the next time you pull."))
        instructions.Wrap(900); sizer.Add(instructions, 0, wx.ALL, 10)
        self.pullList = wx.ListBox(self, choices=[pullRequestLabel(pull) for pull in pulls], style=wx.LB_SINGLE); self.pullList.SetName(_("Open pull requests"))
        if pulls: self.pullList.SetSelection(0)
        sizer.Add(self.pullList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.openButton = wx.Button(self, label=_("&Open in browser")); self.mergeButton = wx.Button(self, label=_("&Merge")); self.squashButton = wx.Button(self, label=_("&Squash and merge")); self.closeButton = wx.Button(self, label=_("C&lose")); self.openButton.SetDefault()
        for button in (self.openButton, self.mergeButton, self.squashButton): buttons.Add(button, 0, wx.RIGHT, 8)
        buttons.Add(self.closeButton, 0); sizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizer(sizer); self.SetMinSize((700, 360)); self.SetSize((1000, 520)); self.CentreOnScreen(); self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def selectedPull(self):
        index = self.pullList.GetSelection()
        return self.pulls[index] if index != wx.NOT_FOUND else None
    def _onKey(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE: self.Close(); return
        if self.pullList.HasFocus() and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.onOpen is not None: self.onOpen()
            return
        event.Skip()

class TranslationReportDialog(wx.Dialog):
    """Show an add-on's translation report with the actions that can fix it."""
    def __init__(self, parent, title, message, canCompile, canWriteTemplate):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL); self.message = _messageField(self, message); sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 15)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.compileButton = wx.Button(self, label=_("&Compile translations")); self.templateButton = wx.Button(self, label=_("Create or &update translation template")); self.closeButton = wx.Button(self, label=_("C&lose"))
        self.compileButton.Enable(canCompile); self.templateButton.Enable(canWriteTemplate)
        for button in (self.compileButton, self.templateButton): buttons.Add(button, 0, wx.RIGHT, 8)
        buttons.Add(self.closeButton, 0); sizer.Add(buttons, 0, wx.ALL, 15)
        self.SetSizer(sizer); self.SetMinSize((600, 320)); self.SetSize((800, 520)); self.CentreOnScreen()
        self.Bind(wx.EVT_CHAR_HOOK, self._onKey)
    def _onKey(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE: self.Close(); return
        event.Skip()

def _messageField(parent, message):
    """A focused, read-only text field: NVDA speaks the first line, and arrows and braille review the rest."""
    field = wx.TextCtrl(parent, value=message, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_RICH2)
    field.SetName(_("Message")); field.SetInsertionPoint(0)
    return field

class ConfirmationDialog(wx.Dialog):
    def __init__(self, parent, title, message, defaultYes=False):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL); self.message = _messageField(self, message); sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 15)
        buttons = wx.BoxSizer(wx.HORIZONTAL); self.yesButton = wx.Button(self, label=_("&Yes")); self.noButton = wx.Button(self, label=_("&No"))
        (self.yesButton if defaultYes else self.noButton).SetDefault(); self.defaultButton = self.yesButton if defaultYes else self.noButton
        buttons.Add(self.yesButton, 0, wx.RIGHT, 8); buttons.Add(self.noButton, 0); sizer.Add(buttons, 0, wx.ALL, 15)
        self.SetSizer(sizer); self.SetMinSize((600, 300)); self.SetSize((760, 420)); self.CentreOnScreen()

class InformationDialog(wx.Dialog):
    def __init__(self, parent, title, message):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL); self.message = _messageField(self, message)
        sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 15); self.okButton = wx.Button(self, label=_("&OK")); self.okButton.SetDefault(); sizer.Add(self.okButton, 0, wx.ALL, 15)
        self.SetSizer(sizer); self.SetMinSize((550, 260)); self.SetSize((720, 380)); self.CentreOnScreen()

class OperationProgressDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title=_("Add-on Developer Updater progress"), style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        initialStatus = _("Working. Each step is shown here as it starts.")
        self.status = wx.TextCtrl(self, value=initialStatus, style=wx.TE_READONLY); self.status.SetName(initialStatus)
        sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 10)
        # The bar stays hidden until real step counts exist, so NVDA never
        # beeps or speaks a percentage that does not measure progress.
        self.gauge = wx.Gauge(self, range=100); self.gauge.SetValue(0); self.gauge.Hide(); sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10); self.SetSizerAndFit(sizer)
    def setProgress(self, percent):
        if not self.gauge.IsShown(): self.gauge.Show(); self.Layout(); self.Fit()
        self.gauge.SetValue(percent)
    def setStatus(self, message):
        """Keep the focused control's value and accessibility name synchronized."""
        message = _(message); self.status.SetName(message); self.status.ChangeValue(message); self.status.SetInsertionPoint(0); self.status.Refresh(); self.status.Update()
        # A focused read-only edit can retain NVDA's old cached value even after
        # WM_SETTEXT. Send the native value-change event so braille follows speech.
        try: ctypes.windll.user32.NotifyWinEvent(0x800E, int(self.status.GetHandle()), -4, 0)
        except (AttributeError, OSError, TypeError, ValueError): pass
