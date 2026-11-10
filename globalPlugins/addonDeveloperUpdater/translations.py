"""Check, template, and compile the gettext translations of an NVDA add-on project.

NVDA add-ons keep one catalog per language in ``locale/LANGUAGE/LC_MESSAGES/nvda.po``
and ship the compiled ``nvda.mo`` beside it.  This module reads and writes those
files directly; it never imports or runs add-on code.
"""

from __future__ import annotations

import ast, json, re, struct
from dataclasses import dataclass, field
from pathlib import Path

GETTEXT_FUNCTIONS = {"_": (0, None, None), "pgettext": (1, None, 0), "ngettext": (0, 1, None), "npgettext": (1, 2, 0)}
"""Function name -> (message argument, plural argument, context argument) positions."""
SOURCE_FOLDERS = ("globalPlugins", "appModules", "synthDrivers", "brailleDisplayDrivers", "visionEnhancementProviders")
PROJECT_TEMPLATE_TOOLS = ("tools/extract_messages.py", "sconstruct", "SConstruct")
PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|\{\d*\}|%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[-#0 +]*\d*(?:\.\d+)?[sdifrxXeEgGc]")
CONTEXT_SEPARATOR = "\x04"

@dataclass
class Entry:
    msgid: str
    context: str = ""
    plural: str = ""
    translations: list[str] = field(default_factory=list)
    fuzzy: bool = False
    references: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.context, self.msgid

    @property
    def translated(self) -> bool:
        return not self.fuzzy and bool(self.translations) and all(text for text in self.translations)

@dataclass
class LanguageStatus:
    language: str
    translated: int
    total: int
    fuzzy: int
    problems: list[str]
    compiled: str
    """'current', 'missing', or 'out of date'."""
    manifest_translated: bool

@dataclass
class TranslationReport:
    name: str
    template: Path | None
    template_tool: str
    new_strings: int
    removed_strings: int
    source_strings: int
    languages: list[LanguageStatus]

def _unquote(text: str) -> str:
    return ast.literal_eval(text.strip())

def parse_catalog(path: Path) -> tuple[str, dict[tuple[str, str], Entry]]:
    """Read a .po or .pot file: plurals, contexts, fuzzy flags, and multi-line strings included."""
    entries: dict[tuple[str, str], Entry] = {}; header = ""
    current: dict = {}; section = None

    def finish():
        nonlocal current, section, header
        if "msgid" in current:
            entry = Entry(current["msgid"], current.get("msgctxt", ""), current.get("msgid_plural", ""), [current[key] for key in sorted(k for k in current if k.startswith("msgstr"))], current.get("fuzzy", False), current.get("references", []), current.get("comments", []))
            if entry.msgid == "" and not entry.context: header = entry.translations[0] if entry.translations else ""
            else: entries[entry.key] = entry
        current = {}; section = None

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            if "msgid" in current: finish()
            continue
        if line.startswith("#~"):
            continue  # Obsolete entries are kept by translators for reference only.
        if line.startswith("#"):
            if "msgid" in current and section and section.startswith("msgstr"): finish()
            if line.startswith("#,") and "fuzzy" in line: current["fuzzy"] = True
            elif line.startswith("#:"): current.setdefault("references", []).extend(line[2:].split())
            elif line.startswith("#."): current.setdefault("comments", []).append(line[2:].strip())
            continue
        if line.startswith('"'):
            if section is None: raise ValueError(f"{path}: text without a keyword: {line[:40]}")
            current[section] += _unquote(line); continue
        keyword, _, value = line.partition(" ")
        keyword = keyword.replace("[", "_").rstrip("]") if keyword.startswith("msgstr[") else keyword
        if keyword in ("msgctxt", "msgid") and any(key.startswith("msgstr") for key in current): finish()
        if keyword not in {"msgctxt", "msgid", "msgid_plural"} and not keyword.startswith("msgstr"):
            raise ValueError(f"{path}: unsupported line {line[:40]}")
        section = keyword; current[section] = _unquote(value)
    finish()
    return header, entries

def _translator_comments(lines: list[str], line_number: int) -> list[str]:
    """Return the '# Translators:' comment block just above a source line."""
    block = []; index = line_number - 2
    while index >= 0 and lines[index].strip().startswith("#"):
        block.insert(0, lines[index].strip().lstrip("#").strip()); index -= 1
    start = next((position for position, text in enumerate(block) if text.startswith("Translators:")), None)
    return block[start:] if start is not None else []

def source_files(root: Path) -> list[Path]:
    files = [root / "installTasks.py"] if (root / "installTasks.py").is_file() else []
    for folder in SOURCE_FOLDERS:
        if (root / folder).is_dir(): files += sorted((path for path in (root / folder).rglob("*.py") if "__pycache__" not in path.parts), key=lambda path: path.as_posix().casefold())
    return files

def extract_messages(root: Path) -> dict[tuple[str, str], Entry]:
    """Collect translatable strings from _, pgettext, ngettext, and npgettext calls."""
    found: dict[tuple[str, str], Entry] = {}
    for path in source_files(root):
        source = path.read_text(encoding="utf-8-sig", errors="replace"); lines = source.splitlines()
        try: tree = ast.parse(source)
        except SyntaxError: continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in GETTEXT_FUNCTIONS: continue
            message_index, plural_index, context_index = GETTEXT_FUNCTIONS[node.func.id]
            arguments = node.args
            def text(index):
                return arguments[index].value if index is not None and index < len(arguments) and isinstance(arguments[index], ast.Constant) and isinstance(arguments[index].value, str) else None
            message = text(message_index)
            if not message: continue
            entry = found.setdefault((text(context_index) or "", message), Entry(message, text(context_index) or "", text(plural_index) or ""))
            entry.references.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
            for comment in _translator_comments(lines, node.lineno):
                if comment not in entry.comments: entry.comments.append(comment)
    return found

def _quote(text: str) -> str:
    if "\n" not in text[:-1]: return json.dumps(text, ensure_ascii=False)
    return '""\n' + "\n".join(json.dumps(line, ensure_ascii=False) for line in text.splitlines(keepends=True))

def render_template(messages: dict[tuple[str, str], Entry], project: str) -> str:
    """Render a gettext template in source order, with translator comments and references."""
    header = ['msgid ""', 'msgstr ""', json.dumps(f"Project-Id-Version: {project}\n"), json.dumps("MIME-Version: 1.0\n"), json.dumps("Content-Type: text/plain; charset=UTF-8\n"), json.dumps("Content-Transfer-Encoding: 8bit\n"), json.dumps("Generated-By: Add-on Developer Updater\n")]
    parts = ["\n".join(header)]
    for entry in messages.values():
        lines = [f"#. {comment}" for comment in entry.comments] + [f"#: {' '.join(entry.references)}"]
        if entry.context: lines.append(f"msgctxt {_quote(entry.context)}")
        lines.append(f"msgid {_quote(entry.msgid)}")
        if entry.plural: lines += [f"msgid_plural {_quote(entry.plural)}", 'msgstr[0] ""', 'msgstr[1] ""']
        else: lines.append('msgstr ""')
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"

def find_template(root: Path) -> Path | None:
    """Return an existing template: locale/*.pot first, then *.pot beside the manifest or one folder up."""
    for folder in (root / "locale", root, root.parent):
        templates = sorted(folder.glob("*.pot")) if folder.is_dir() else []
        if templates: return templates[0]
    return None

def template_tool(root: Path, project_root: Path) -> str:
    """Name a project's own template tool, which must not be bypassed."""
    for folder in dict.fromkeys((root, project_root)):
        for name in PROJECT_TEMPLATE_TOOLS:
            if (folder / name).is_file(): return (folder / name).relative_to(project_root).as_posix() if folder == project_root else name
    return ""

def _placeholders(text: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(text.replace("%%", "")))

def _shortcut_keys(text: str) -> int:
    return len(re.findall(r"&(?!&)", text.replace("&&", "")))

def validate(source: str, translation: str) -> list[str]:
    """Return problems that would break or garble one translated string."""
    problems = []
    if _placeholders(source) != _placeholders(translation): problems.append("placeholders differ")
    if _shortcut_keys(source) != _shortcut_keys(translation): problems.append("shortcut key (&) count differs")
    if source.count("\n") != translation.count("\n"): problems.append("line count differs")
    if source[:1].isspace() != translation[:1].isspace() or source[-1:].isspace() != translation[-1:].isspace(): problems.append("leading or trailing space differs")
    return problems

def _compiled_messages(header: str, entries: dict[tuple[str, str], Entry]) -> dict[str, str]:
    """Return the key/value pairs msgfmt writes: context and plural forms joined with NUL and EOT."""
    compiled = {"": header}
    for entry in entries.values():
        if not entry.translated: continue
        key = (entry.context + CONTEXT_SEPARATOR if entry.context else "") + entry.msgid + ("\0" + entry.plural if entry.plural else "")
        compiled[key] = "\0".join(entry.translations)
    return compiled

def compile_catalog(po_path: Path) -> Path:
    """Write nvda.mo beside nvda.po in GNU gettext format, as msgfmt does."""
    header, entries = parse_catalog(po_path)
    messages = _compiled_messages(header, entries); keys = sorted(messages)
    ids = strings = b""; offsets = []
    for key in keys:
        key_bytes, value_bytes = key.encode("utf-8"), messages[key].encode("utf-8")
        offsets.append((len(ids), len(key_bytes), len(strings), len(value_bytes)))
        ids += key_bytes + b"\0"; strings += value_bytes + b"\0"
    count = len(keys); key_start = 7 * 4 + 16 * count; value_start = key_start + len(ids)
    table = [value for id_offset, id_length, _s, _l in offsets for value in (id_length, id_offset + key_start)]
    table += [value for _i, _l, string_offset, string_length in offsets for value in (string_length, string_offset + value_start)]
    output = struct.pack("Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + count * 8, 0, 0) + struct.pack(f"{len(table)}i", *table) + ids + strings
    mo_path = po_path.with_suffix(".mo"); mo_path.write_bytes(output)
    return mo_path

def _read_mo(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    magic = struct.unpack_from("<I", data)[0]
    order = "<" if magic == 0x950412DE else ">"
    _magic, _revision, count, keys_offset, values_offset = struct.unpack_from(order + "5I", data)
    result = {}
    for index in range(count):
        key_length, key_offset = struct.unpack_from(order + "2I", data, keys_offset + index * 8)
        value_length, value_offset = struct.unpack_from(order + "2I", data, values_offset + index * 8)
        result[data[key_offset:key_offset + key_length].decode("utf-8")] = data[value_offset:value_offset + value_length].decode("utf-8")
    return result

def compiled_state(po_path: Path) -> str:
    """Say whether nvda.mo matches nvda.po: 'current', 'missing', or 'out of date'."""
    mo_path = po_path.with_suffix(".mo")
    if not mo_path.is_file(): return "missing"
    try:
        header, entries = parse_catalog(po_path); expected = _compiled_messages(header, entries); compiled = _read_mo(mo_path)
    except (OSError, ValueError, struct.error, UnicodeError):
        return "out of date"
    return "current" if all(compiled.get(key) == value for key, value in expected.items() if key) and set(compiled) - {""} <= set(expected) else "out of date"

def check_project(manifest: Path, project_root: Path | None = None, name: str = "") -> TranslationReport:
    """Report the template's freshness and every language's completeness and problems."""
    root = manifest.parent; project_root = project_root or root
    template = find_template(root); tool = template_tool(root, project_root)
    source = extract_messages(root)
    template_entries = parse_catalog(template)[1] if template else {}
    reference = template_entries or source
    # A project's own tool may extract strings this generic reader cannot see, so its template is trusted.
    new = 0 if tool and template else (len(set(source) - set(template_entries)) if template else len(source))
    removed = 0 if tool and template else (len(set(template_entries) - set(source)) if template else 0)
    languages = []
    for po_path in sorted((root / "locale").glob("*/LC_MESSAGES/nvda.po")) if (root / "locale").is_dir() else []:
        language = po_path.parents[1].name
        try: _header, entries = parse_catalog(po_path)
        except (OSError, ValueError, SyntaxError, UnicodeError) as error:
            languages.append(LanguageStatus(language, 0, len(reference), 0, [f"nvda.po cannot be read: {error}"], "out of date", False)); continue
        problems = []
        for key, entry in entries.items():
            if not entry.translated: continue
            sources = [entry.msgid] + ([entry.plural] * (len(entry.translations) - 1) if entry.plural else [])
            for index, translation in enumerate(entry.translations):
                for problem in validate(sources[min(index, len(sources) - 1)], translation):
                    problems.append(f"{problem}: {entry.msgid[:60]}")
        languages.append(LanguageStatus(language, sum(1 for key in reference if key in entries and entries[key].translated), len(reference), sum(1 for key in reference if key in entries and entries[key].fuzzy), problems, compiled_state(po_path), (po_path.parents[1] / "manifest.ini").is_file()))
    return TranslationReport(name or root.name, template, tool, new, removed, len(source), languages)

def report_lines(report: TranslationReport, translate=lambda text: text) -> tuple[str, list[str]]:
    """Return a plain-language summary line and one detail per line, for speech and braille."""
    _ = translate; details = []
    problems = sum(len(language.problems) + (language.compiled != "current") for language in report.languages)
    languages = _("1 language") if len(report.languages) == 1 else _("%d languages") % len(report.languages)
    if not report.languages: summary = _("%s has no translations yet.") % report.name
    elif problems: summary = _("%s: %s, %s to fix.") % (report.name, languages, _("1 problem") if problems == 1 else _("%d problems") % problems)
    else: summary = _("%s: %s, no problems found.") % (report.name, languages)
    if report.template is not None and report.template_tool:
        details.append(_("The translation template is built by this project's own tool, %s. Run that tool after changing translatable text.") % report.template_tool)
    elif report.template is None:
        details.append(_("No translation template (.pot) exists yet; %d strings in the code can be translated.") % report.source_strings)
        if report.template_tool: details.append(_("This project has its own template tool, %s; run it instead of creating the template here.") % report.template_tool)
    elif report.new_strings or report.removed_strings:
        details.append(_("The translation template is out of date: %d new strings in the code are missing from it, and %d strings in it are no longer used.") % (report.new_strings, report.removed_strings))
    else:
        details.append(_("The translation template is up to date with %d strings.") % report.source_strings)
    for language in report.languages:
        state = _("%s: %d of %d strings translated") % (language.language, language.translated, language.total)
        if language.fuzzy: state += _(", %d need review") % language.fuzzy
        if language.compiled == "missing": state += _("; the compiled nvda.mo file is missing")
        elif language.compiled == "out of date": state += _("; the compiled nvda.mo file is out of date")
        if not language.manifest_translated: state += _("; add-on name and description not translated")
        details.append(state)
        details += [_("  %s problem: %s") % (language.language, problem) for problem in language.problems[:10]]
        if len(language.problems) > 10: details.append(_("  and %d more problems in %s") % (len(language.problems) - 10, language.language))
    return summary, details

def publish_issues(manifest: Path) -> list[str]:
    """Return translation problems that would ship broken or stale text in a release package."""
    try: report = check_project(manifest)
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        return [f"translations could not be checked: {error}"]
    issues = [f"{language.language} nvda.mo is {language.compiled}; compile translations before releasing" for language in report.languages if language.compiled != "current"]
    issues += [f"{language.language} has {len(language.problems)} translation problems such as {language.problems[0]}" for language in report.languages if language.problems]
    return issues
