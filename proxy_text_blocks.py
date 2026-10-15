#!/usr/bin/env python3
"""
Text-block utilities for proxy YAML manipulation.

Instead of parsing YAML → dict → re-dump (which destroys field order and formatting),
we treat each proxy node as an opaque text block and only modify the "name" line.

This preserves the EXACT original formatting from upstream sources.
"""

import re
import hashlib
from collections import OrderedDict


def split_proxy_blocks(yaml_text):
    """
    Split a YAML text into (header, proxy_blocks) where:
      - header: everything before the first '  - name:' line
      - proxy_blocks: list of text blocks, each starting with '  - name: ...'

    Each block is a complete proxy node text including all nested lines.
    Blocks are separated by the next '  - name:' or EOF.
    """
    lines = yaml_text.split('\n')
    header_lines = []
    blocks = []
    current_block = None

    for line in lines:
        # Detect start of a new proxy block: "  - name:" (indent 2) or "- name:" (indent 0)
        if re.match(r'^  - name:', line) or re.match(r'^- name:', line):
            if current_block is not None:
                blocks.append('\n'.join(current_block))
            current_block = [line]
        elif current_block is not None:
            # Check if we've left the proxy block (back to top-level key or end of proxies section)
            # A new proxy block starts with "  - name:" or "- name:", top-level keys have no indent
            # If we see a line with indent < 2 and it's not empty and not a continuation of
            # the current block's nested structure, we've left proxies section.
            stripped = line.lstrip()
            if stripped and not line.startswith('    ') and not line.startswith('  -') and not line.startswith('  ') and not line.startswith('- '):
                # This is a new top-level section → end of proxies
                blocks.append('\n'.join(current_block))
                current_block = None
                header_lines.append(line)
                continue
            current_block.append(line)
        else:
            header_lines.append(line)

    # Don't forget the last block
    if current_block is not None:
        blocks.append('\n'.join(current_block))

    header = '\n'.join(header_lines)
    return header, blocks


def extract_proxy_blocks_from_file(filepath):
    """
    Read a YAML file and extract proxy text blocks from the 'proxies:' section.

    Handles:
    - Standard YAML with 'proxies:' key
    - Files where proxies are just a list (no 'proxies:' key)
    - Base64-encoded content

    Returns (header_text, proxy_blocks_list)
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Try base64 decode if content doesn't look like YAML
    if not content.strip().startswith('#') and not content.strip().startswith('mixed') and \
       not content.strip().startswith('port:') and not content.strip().startswith('proxies') and \
       not content.strip().startswith('-'):
        import base64
        try:
            decoded = base64.b64decode(content).decode('utf-8')
            content = decoded
        except Exception:
            pass

    # Split into proxies section and the rest
    # Find the "proxies:" line
    proxies_match = re.search(r'^proxies:\s*$', content, re.MULTILINE)
    if proxies_match:
        header = content[:proxies_match.end()]
        body = content[proxies_match.end():]
        # Prepend "proxies:\n" to the body so split_proxy_blocks works
        _, blocks = split_proxy_blocks(body)
    else:
        # No "proxies:" key — maybe the whole file is just proxy blocks
        # Check if it starts with "  - name:" directly
        if re.match(r'^  - name:', content.strip()):
            header = 'proxies:\n'
            _, blocks = split_proxy_blocks(content)
        else:
            # Try to find "- name:" at any indent level
            header = ''
            _, blocks = split_proxy_blocks(content)

    return header, blocks


def get_block_name(block_text):
    """Extract the 'name' field value from a proxy text block, stripping YAML quotes."""
    m = re.search(r'^(?:  )?- name:\s*(.+)$', block_text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    # Strip YAML single/double quotes
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        val = val[1:-1]
    return val


def get_block_server(block_text):
    """Extract the 'server' field value from a proxy text block."""
    m = re.search(r'^\s+server:\s*(.+)$', block_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def get_block_port(block_text):
    """Extract the 'port' field value from a proxy text block."""
    m = re.search(r'^\s+port:\s*(\d+)', block_text, re.MULTILINE)
    return int(m.group(1)) if m else None


def get_block_type(block_text):
    """Extract the 'type' field value from a proxy text block."""
    m = re.search(r'^\s+type:\s*(.+)$', block_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def get_block_field(block_text, field_name):
    """Extract any scalar field from a proxy text block."""
    m = re.search(rf'^\s+{re.escape(field_name)}:\s*(.+)$', block_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def block_hash(block_text):
    """
    Unique hash for a proxy block: server:port:type:password/uuid.
    Used for deduplication.
    """
    server = get_block_server(block_text) or ''
    port = get_block_port(block_text) or ''
    ptype = get_block_type(block_text) or ''
    secret = get_block_field(block_text, 'password') or get_block_field(block_text, 'uuid') or ''
    key = f"{server}:{port}:{ptype}:{secret}"
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def set_block_name(block_text, new_name):
    """
    Replace the 'name' field in a proxy text block.
    Preserves everything else exactly as-is.

    Args:
        block_text: The original text block
        new_name: New name value (without quotes — will be quoted if needed)

    Returns:
        Modified text block with name replaced
    """
    # Check if the name needs quoting (contains special YAML chars)
    if any(c in new_name for c in [':', '#', '{', '}', '[', ']', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"', "'"]):
        # Quote the name
        new_name = f"'{new_name}'"

    # Replace only the first occurrence of "  - name: ..." or "- name: ..."
    return re.sub(
        r'^((?:  )?- name:).*$',
        rf'\1 {new_name}',
        block_text,
        count=1,
        flags=re.MULTILINE
    )


def blocks_to_proxies_yaml(header, blocks):
    """
    Reassemble header + sorted blocks into a complete YAML string.
    """
    parts = [header.rstrip()]
    parts.extend(blocks)
    return '\n'.join(parts) + '\n'


def deduplicate_blocks(blocks):
    """
    Deduplicate proxy blocks by server:port:type:secret hash.
    Preserves insertion order (first occurrence wins).
    """
    seen = set()
    unique = []
    for block in blocks:
        h = block_hash(block)
        if h and h not in seen:
            seen.add(h)
            unique.append(block)
    return unique


if __name__ == '__main__':
    # Quick test
    import sys
    if len(sys.argv) > 1:
        header, blocks = extract_proxy_blocks_from_file(sys.argv[1])
        print(f"Header length: {len(header)} chars")
        print(f"Blocks: {len(blocks)}")
        for i, b in enumerate(blocks[:3]):
            name = get_block_name(b)
            server = get_block_server(b)
            port = get_block_port(b)
            print(f"  [{i}] name={name} server={server}:{port}")
            print(f"       hash={block_hash(b)}")
            print()
        if len(sys.argv) > 2:
            # Test rename
            new_name = sys.argv[2]
            modified = set_block_name(blocks[0], new_name)
            print(f"=== Modified block ===")
            print(modified)
