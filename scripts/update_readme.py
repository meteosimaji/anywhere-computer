"""Render shared README references from package metadata; --check is used by CI."""

import argparse
import ast
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from package_plugin import plugin_version

ROOT = Path(__file__).resolve().parents[1]
START = '<!-- BEGIN GENERATED: project-reference -->'
END = '<!-- END GENERATED: project-reference -->'
GUIDES = (
    ('docs/PORTABLE.md', 'Downloads and portable installation', '配布物と導入'),
    ('docs/MCP-CLIENTS.md', 'MCP client configuration', 'MCPクライアントの設定'),
    ('docs/SETUP-CONTROLLER.md', 'Guided setup and ChatGPT connection', '対話式設定とChatGPT接続'),
    ('docs/OPERATIONS.md', 'Commands and diagnosis', 'コマンドと診断'),
    ('docs/UPDATING.md', 'Updates and recovery', '更新と復旧'),
    ('docs/DEVICE-ROUTING.md', 'Multiple computers', '複数PCの操作'),
    ('docs/GUI-MCP.md', 'GUI provider setup and limits', 'GUIの導入条件と制限'),
    ('docs/SUBCHAT-PROBE.md', 'Subchat usage and limits', 'subchatの使い方と制限'),
    ('docs/PRODUCT-ROADMAP.md', 'Future milestones', '今後の開発方針'),
    ('docs/DOCUMENTATION.md', 'Documentation ownership and checks', '文書の正本と更新方法'),
)


def reference(language: int) -> str:
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    version_path = project['tool']['hatch']['version']['path']
    tree = ast.parse((ROOT / version_path).read_text(encoding='utf-8'))
    versions = [ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == '__version__'
                    for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError('Expected one literal package version')
    version = versions[0]
    heading = ('This checkout (not a publication or installed-runtime claim):',
               'このチェックアウトの情報（公開済み・稼働中の版を示すものではありません）:')[language]
    rows = [START, heading, '', '| Source | Value |', '| --- | --- |',
            f'| [Python package]({version_path}) | `{version}` |',
            f'| [Codex Plugin version mapping](scripts/package_plugin.py) | '
            f'`{plugin_version(version)}` |',
            f'| [Python requirement](pyproject.toml) | '
            f'`{project["project"]["requires-python"]}` |', '',
            ('Canonical guides:', '各項目の正本:')[language], '']
    rows.extend(f'- [{entry[language + 1]}]({entry[0]})' for entry in GUIDES)
    return '\n'.join([*rows, END])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail instead of writing stale blocks')
    args = parser.parse_args()
    problems = []
    for language, name in enumerate(('README.md', 'README.ja.md')):
        path = ROOT / name
        text = path.read_text(encoding='utf-8')
        if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
            raise ValueError(f'{name}: expected one ordered generated block')
        updated = text[:text.index(START)] + reference(language) + text[text.index(END) + len(END):]
        if text != updated:
            if args.check:
                problems.append(f'{name}: run python scripts/update_readme.py')
            else:
                path.write_text(updated, encoding='utf-8')
                print(f'Updated {name}')
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', updated):
            link = urlsplit(target)
            if not link.scheme and not link.netloc and link.path:
                if not (path.parent / unquote(link.path)).exists():
                    problems.append(f'{name}: missing local link target {target}')
    if problems:
        raise SystemExit('\n'.join(problems))
    print('README references and local link targets are current.')


if __name__ == '__main__':
    main()
