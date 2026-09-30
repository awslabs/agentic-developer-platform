#!/usr/bin/env python3
"""Check literal account references without treating HCL comments as values."""
import pathlib
import re
import sys


def quoted_end(text, pos):
    """Consume a template string, including nested strings in interpolations."""
    pos += 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
        elif text[pos] == '"':
            return pos + 1
        elif text[pos:pos + 3] in ("$${", "%%{"):
            pos += 3
        elif text[pos:pos + 2] in ("${", "%{"):
            pos += 2
            depth = 1
            while pos < len(text) and depth:
                if text[pos] == '"':
                    pos = quoted_end(text, pos)
                    continue
                if text[pos] == '{':
                    depth += 1
                elif text[pos] == '}':
                    depth -= 1
                pos += 1
            if depth:
                raise ValueError('Unterminated HCL template expression')
        else:
            pos += 1
    raise ValueError('Unterminated HCL string')


def values_without_comments(text):
    # Preserve string/template and heredoc bodies. Comment markers inside them
    # are values, including URL fragments and IAM policy document contents.
    token = re.compile(r'"|<<(-?)([A-Za-z_][A-Za-z0-9_]*)[^\n]*\n|#[^\n]*|//[^\n]*|/\*[\s\S]*?\*/')
    out = []
    pos = 0
    while match := token.search(text, pos):
        out.append(text[pos:match.start()])
        value = match.group()
        if value.startswith('<<'):
            ending = re.compile(r'(?m)^' + (r'[ \t]*' if match[1] else '') + re.escape(match[2]) + r'[ \t]*\r?$').search(text, match.end())
            if ending is None:
                raise ValueError('Unterminated HCL heredoc')
            pos = ending.end()
            out.append(text[match.start():pos])
        elif value == '"':
            pos = quoted_end(text, match.start())
            out.append(text[match.start():pos])
        else:
            out.append('\n')
            pos = match.end()
    out.append(text[pos:])
    return ''.join(out)


def check(filename, account):
    path = pathlib.Path(filename).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'Upgrade tfvars file does not exist: {path}')
    foreign = set(re.findall(r'(?<![0-9])[0-9]{12}(?![0-9])', values_without_comments(path.read_text()))) - {account}
    if foreign:
        raise ValueError(f'Upgrade tfvars {path} references a different AWS account; provide target-specific tfvars')
    return path


if __name__ == '__main__':
    try:
        print(check(*sys.argv[1:]))
    except ValueError as error:
        raise SystemExit(str(error))
