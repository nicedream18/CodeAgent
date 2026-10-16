"""Explicit tutorial data; never selected by the generic runtime."""

AMIYA_TODOS = [
    "Research Amiya and collect reliable source links.",
    "Create amiya_profile.html with a polished character introduction.",
    "Include at least two source links in the HTML.",
    "Run non-interactive checks for the generated HTML file.",
]

AMIYA_CRITERIA = [
    "amiya_profile.html exists in the workspace.",
    "The page mentions 阿米娅 and 明日方舟.",
    "The page introduces identity, traits, abilities, and story role.",
    "The page includes at least two source links.",
]

AMIYA_COMMANDS = [
    "python -c \"from pathlib import Path; p=Path('amiya_profile.html'); s=p.read_text(encoding='utf-8'); assert '阿米娅' in s and '明日方舟' in s; assert s.lower().count('http') >= 2; print('amiya html ok')\"",
]
