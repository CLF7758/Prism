# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Tag model: hierarchy, namespaces, synonyms and combined queries.

A tag stays a plain string on every item, so no file format had to change
for any of this.  The structure is a naming convention plus one small
mapping table that is saved inside the project:

``/``
    Separates hierarchy levels.  ``建筑/红墙`` is a child of ``建筑``.
    The parent does not have to exist as a tag of its own; the tree shows
    it as a node that is only implied by its children.
``:``
    Separates a namespace from the rest of a level, as in ``场景:雨夜``.
    Namespaces are used for colouring and for wildcard searches.
``*``
    Wildcard inside a search term: ``场景:*``.
``-``
    In front of a search term, excludes it: ``建筑 -草图``.
``|`` / ``,``
    Between search terms, means "either one": ``雨夜 | 雪天``.

Synonyms (``siblings``) map an alias onto the tag it really means, e.g.
``霓虹`` -> ``neon``.  Implied parents (``parents``) record "tagging X also
means Y" for tags that are not nested, e.g. ``红墙`` -> ``建筑``.  Both are
virtual: no item is ever given a tag it did not receive, and the original
tag is kept on the item - only the display, the counts and the search fold
them together.

Nothing in this module imports Qt, so the model can be tested on its own.
"""

import fnmatch
import re
import zlib

PATH_SEP = '/'
NAMESPACE_SEP = ':'
WILDCARD = '*'
NEGATION = '-'

#: Guards against a mapping cycle turning a walk into an endless loop.
MAX_DEPTH = 64

OR_TOKENS = {'|', '，', ',', 'or'}

#: Colours picked for namespaces; the same namespace always gets the same
#: one, so a project's colours stay recognisable between sessions.
NAMESPACE_COLORS = (
    '#4aa3ff', '#ff9f0a', '#30d158', '#bf5af2',
    '#ff453a', '#5ac8fa', '#ffd60a', '#ff375f',
)


# ── Naming conventions ────────────────────────────────────────────────

def split_path(tag):
    """Split a tag into its hierarchy levels, dropping empty ones."""
    return [part.strip() for part in str(tag).split(PATH_SEP) if part.strip()]


def join_path(parts):
    """Inverse of :func:`split_path`."""
    return PATH_SEP.join(part for part in parts if str(part).strip())


def leaf(tag):
    """The last level of a tag: the name shown last in the tree."""
    parts = split_path(tag)
    return parts[-1] if parts else str(tag).strip()


def namespace(tag):
    """Namespace of a tag, or an empty string when it has none.

    Only the first level carries a namespace, so ``场景:雨夜/内景`` still
    belongs to ``场景``.
    """
    head = str(tag).split(PATH_SEP, 1)[0]
    if NAMESPACE_SEP in head:
        return head.split(NAMESPACE_SEP, 1)[0].strip()
    return ''


def namespace_value(tag):
    """The part of the leaf after the namespace separator."""
    last = leaf(tag)
    if NAMESPACE_SEP in last:
        return last.split(NAMESPACE_SEP, 1)[1].strip()
    return ''


def namespace_color(tag_or_namespace):
    """A stable colour for a namespace, or '' when there is none."""
    name = namespace(tag_or_namespace)
    if not name:
        return ''
    index = zlib.crc32(name.encode('utf-8')) % len(NAMESPACE_COLORS)
    return NAMESPACE_COLORS[index]


def ancestors(tag):
    """Every strict ancestor path of a tag, closest one first."""
    parts = split_path(tag)
    return [join_path(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


# ── Query language ────────────────────────────────────────────────────

class Term:
    """One search term: a tag name, optionally with ``*`` and a ``-``."""

    __slots__ = ('pattern', 'negated', 'regex')

    def __init__(self, pattern, negated=False):
        self.pattern = str(pattern).strip().casefold()
        self.negated = negated
        self.regex = None
        if not self.pattern:
            raise ValueError('empty tag search term')
        if WILDCARD in self.pattern:
            self.regex = re.compile(fnmatch.translate(self.pattern))

    def __repr__(self):
        return f'Term({self.pattern!r}, negated={self.negated})'

    def matches(self, index):
        """Whether one tag of the index matches this term.

        A trailing ``*`` also covers everything below a namespace or a
        level, and an exact term matches any level of a tag - searching
        ``红墙`` finds ``建筑/红墙`` as well as ``红墙`` itself.
        """
        if self.regex is not None:
            hit = any(self.regex.match(name) for name in index)
        else:
            hit = self.pattern in index
        return hit != self.negated


class TagQuery:
    """A parsed tag search: groups of terms OR-ed together, AND-ed.

    ``场景:* 建筑`` needs both a tag from the ``场景`` namespace and the
    tag ``建筑``; ``雨夜 | 雪天`` needs either one; ``-草图`` rules the
    term out.
    """

    def __init__(self, groups=None, text=''):
        self.groups = groups or []
        self.text = text

    def __bool__(self):
        return bool(self.groups)

    def __repr__(self):
        return f'TagQuery({self.text!r})'

    @property
    def terms(self):
        return [term for group in self.groups for term in group]

    def matches(self, index):
        """Whether a set of tag names (see :func:`name_index`) matches."""
        for group in self.groups:
            if not any(term.matches(index) for term in group):
                return False
        return True


def parse_query(text):
    """Parse a query string into a :class:`TagQuery`.

    An empty string gives an empty query, which matches everything.
    """
    groups = []
    pending_or = False
    for token in _tokenize(text):
        if token.lower() in OR_TOKENS:
            # '雨夜 | 雪天' reads as one group with two ways to match.
            pending_or = True
            continue
        alternatives = []
        for part in _split_or(token):
            part = part.strip().strip('"').strip()
            if not part or part == NEGATION:
                continue
            negated = part.startswith(NEGATION)
            pattern = part[1:] if negated else part
            if pattern:
                alternatives.append(Term(pattern, negated))
        if not alternatives:
            continue
        if groups and pending_or:
            groups[-1].extend(alternatives)
        else:
            groups.append(alternatives)
        pending_or = False
    return TagQuery(groups, str(text or ''))


def _tokenize(text):
    """Split on whitespace, keeping quoted phrases in one piece."""
    tokens = []
    current = ''
    quoted = False
    for char in str(text or ''):
        if char == '"':
            quoted = not quoted
            current += char
        elif char.isspace() and not quoted:
            if current:
                tokens.append(current)
            current = ''
        else:
            current += char
    if current:
        tokens.append(current)
    return tokens


def _split_or(chunk):
    """Split one query chunk on ``|`` and ``,`` outside of quotes."""
    parts = []
    current = ''
    quoted = False
    for char in chunk:
        if char == '"':
            quoted = not quoted
            current += char
        elif char in '|,，' and not quoted:
            parts.append(current)
            current = ''
        else:
            current += char
    parts.append(current)
    return parts


# ── Expanding tags for search ─────────────────────────────────────────

def name_index(tags, system):
    """Every name a set of tags can be found by.

    A single tag contributes its full path, each level of that path, the
    leaf with and without its namespace, and all of that again for every
    synonym of the tag.  Implied parents are added the same way, so
    ``建筑`` finds an item that only carries ``建筑/红墙``.
    """
    index = set()
    for tag in tags or ():
        tag = str(tag).strip()
        if not tag:
            continue
        _add_tag_names(index, tag, system)
        for parent in system.implied_parents(tag):
            _add_tag_names(index, parent, system)
    return index


def _add_tag_names(index, tag, system):
    for name in system.group(tag):
        index.add(name.casefold())
        index.add(leaf(name).casefold())
        ns = namespace(name)
        if ns:
            index.add(ns.casefold())
        value = namespace_value(name)
        if value:
            index.add(value.casefold())
        for ancestor in ancestors(name):
            index.add(ancestor.casefold())
            index.add(leaf(ancestor).casefold())


# ── The mapping table ─────────────────────────────────────────────────

class TagSystem:
    """Synonym and implied-parent mappings of one project.

    The table only ever names tags; it never touches the items.  Keys are
    compared exactly, the same way tags are compared everywhere else.
    """

    def __init__(self, siblings=None, parents=None):
        self._siblings = {}
        self._parents = {}
        self._groups = {}
        self.set_siblings(siblings or ())
        self.set_parents(parents or ())

    # ── persistence ───────────────────────────────────────────────────

    def to_dict(self):
        """Plain data, ready to be stored in the project file."""
        return {
            'siblings': sorted([alias, canonical]
                               for alias, canonical in self._siblings.items()),
            'parents': sorted([child, parent]
                              for child, parents in self._parents.items()
                              for parent in parents),
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild from stored data; anything unreadable is ignored."""
        if isinstance(data, cls):
            return data
        siblings = []
        parents = []
        if isinstance(data, dict):
            for entry in data.get('siblings') or []:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    siblings.append((entry[0], entry[1]))
            for entry in data.get('parents') or []:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    parents.append((entry[0], entry[1]))
        return cls(siblings, parents)

    def copy(self):
        clone = TagSystem()
        clone._siblings = dict(self._siblings)
        clone._parents = {child: set(parents)
                          for child, parents in self._parents.items()}
        return clone

    def is_empty(self):
        return not self._siblings and not self._parents

    # ── synonyms ──────────────────────────────────────────────────────

    def siblings(self):
        """Alias -> the tag it is folded into."""
        return dict(self._siblings)

    def set_siblings(self, pairs):
        self._siblings = {}
        self._groups = {}
        for alias, canonical in pairs:
            alias = str(alias or '').strip()
            canonical = str(canonical or '').strip()
            if alias and canonical and alias != canonical:
                self._siblings[alias] = canonical

    def add_sibling(self, alias, canonical):
        """Make ``alias`` another name for ``canonical``."""
        alias = str(alias or '').strip()
        canonical = str(canonical or '').strip()
        if not alias or not canonical or alias == canonical:
            return False
        if canonical in self.group(alias):
            # Would create a cycle: neither direction wins.
            return False
        self._siblings[alias] = canonical
        # An alias that was the target of others follows along.
        for other, target in list(self._siblings.items()):
            if target == alias:
                self._siblings[other] = canonical
        self._groups = {}
        return True

    def remove_sibling(self, alias):
        removed = bool(self._siblings.pop(str(alias or '').strip(), None))
        if removed:
            self._groups = {}
        return removed

    def canonical(self, tag):
        """Follow the synonym chain to the name a tag is filed under."""
        name = str(tag or '').strip()
        for _ in range(MAX_DEPTH):
            target = self._siblings.get(name)
            if not target or target == name:
                break
            name = target
        return name

    def group(self, tag):
        """Every name that means the same tag: the canonical one and its
        aliases."""
        tag = str(tag or '').strip()
        cached = self._groups.get(tag)
        if cached is None:
            canonical = self.canonical(tag)
            cached = sorted([canonical] + [name for name in self._siblings
                                           if self.canonical(name) == canonical])
            self._groups[tag] = cached
        return cached

    def aliases(self, tag):
        """Only the other names of a tag, without the canonical one."""
        canonical = self.canonical(tag)
        return [name for name in self.group(canonical) if name != canonical]

    def other_names(self, tag):
        """The other names of a tag, whichever of them is asked about.

        ``aliases`` works from the canonical name; this one also answers
        for an alias itself, which is what a row showing that alias needs.
        """
        name = str(tag or '').strip()
        return [other for other in self.group(name) if other != name]

    # ── implied parents ───────────────────────────────────────────────

    def parents(self):
        """Child -> the tags it implies."""
        return {child: set(parents)
                for child, parents in self._parents.items()}

    def set_parents(self, pairs):
        self._parents = {}
        for child, parent in pairs:
            child = str(child or '').strip()
            parent = str(parent or '').strip()
            if child and parent and child != parent:
                self._parents.setdefault(child, set()).add(parent)

    def add_parent(self, child, parent):
        """Tagging ``child`` also counts as tagging ``parent``."""
        child = str(child or '').strip()
        parent = str(parent or '').strip()
        if not child or not parent or child == parent:
            return False
        if child in self.implied_parents(parent):
            return False
        self._parents.setdefault(child, set()).add(parent)
        return True

    def remove_parent(self, child, parent):
        parents = self._parents.get(str(child or '').strip())
        if not parents:
            return False
        removed = str(parent or '').strip() in parents
        parents.discard(str(parent or '').strip())
        if not parents:
            self._parents.pop(str(child or '').strip(), None)
        return removed

    def rename_prefix(self, old, new):
        """Carry a rename or a move through the whole mapping table.

        Renaming ``建筑`` to ``构筑`` has to touch ``建筑/红墙`` in the
        synonyms and in the implied parents as well, otherwise the table
        would keep pointing at a tag that no longer exists.
        """
        old = str(old or '').strip()
        new = str(new or '').strip()
        if not old or not new or old == new:
            return

        def rename(name):
            if name == old:
                return new
            if is_descendant(name, old):
                tail = split_path(name)[len(split_path(old)):]
                return join_path(split_path(new) + tail)
            return name

        siblings = {}
        for alias, canonical in self._siblings.items():
            alias, canonical = rename(alias), rename(canonical)
            if alias and canonical and alias != canonical:
                siblings[alias] = canonical
        self._siblings = siblings
        self._groups = {}

        parents = {}
        for child, targets in self._parents.items():
            child = rename(child)
            renamed = {rename(target) for target in targets} - {child}
            renamed.discard('')
            if child and renamed:
                parents[child] = renamed
        self._parents = parents

    def direct_parents(self, tag):
        """The level above a tag plus the parents it was given explicitly."""
        name = str(tag or '').strip()
        parents = set()
        upper = ancestors(name)
        if upper:
            parents.add(upper[0])
        parents |= set(self._parents.get(name, ()))
        parents.discard(name)
        return parents

    def implied_parents(self, tag):
        """Everything a tag also counts as, following both chains."""
        name = str(tag or '').strip()
        found = set()
        seen = {name}
        pending = [name]
        while pending and len(seen) <= 512:
            for parent in self.direct_parents(pending.pop()):
                if parent in seen:
                    continue
                seen.add(parent)
                found.add(parent)
                pending.append(parent)
        return found


def tag_system(scene):
    """The tag system of a project, created on first use.

    It is kept on the scene so that saving only has to read one attribute.
    """
    system = getattr(scene, 'tag_system', None)
    if not isinstance(system, TagSystem):
        system = TagSystem.from_dict(system)
        try:
            scene.tag_system = system
        except AttributeError:  # pragma: no cover - a scene always takes it
            pass
    return system


def tag_names_of_items(scene):
    """The tags that are actually in use on the board, with their counts."""
    getter = getattr(scene, 'get_all_tags', None)
    if callable(getter):
        return dict(getter())
    counts = {}
    for item in getattr(scene, 'items', list)():
        for name in getattr(item, '_tags', []) or []:
            counts[name] = counts.get(name, 0) + 1
    return counts


def scene_tag_names(scene):
    """Every tag name the project knows about: catalogue, items and pages."""
    names = set(getattr(scene, 'tag_names', []) or [])
    for page in getattr(scene, 'workspace_pages', []) or []:
        names.update(page.get('tags', []) or [])
    names.update(tag_names_of_items(scene))
    names = {str(name).strip() for name in names}
    names.discard('')
    return sorted(names, key=str.casefold)


# ── The tree shown in the sidebar ─────────────────────────────────────

class TagNode:
    """One row of the tag tree.

    ``count`` includes everything below the node and everything the node
    implies, which is the number a search with that tag would return.
    """

    __slots__ = ('name', 'label', 'depth', 'count', 'direct', 'asset',
                 'aliases', 'namespace', 'color', 'children', 'parent')

    def __init__(self, name):
        self.name = name
        self.label = leaf(name)
        self.depth = 0
        self.count = 0
        self.direct = 0
        #: How many of ``direct`` are assets.  A tag that only pages carry
        #: has a count but no asset behind it, which is what the sidebar
        #: marks as "page only" - filtering assets can never find it.
        self.asset = 0
        self.aliases = []
        self.namespace = namespace(name)
        self.color = namespace_color(name)
        self.children = []
        self.parent = None

    def __repr__(self):
        return f'TagNode({self.name!r}, depth={self.depth}, count={self.count})'

    @property
    def virtual(self):
        """True when no item carries this tag itself."""
        return self.direct == 0


def build_tree(names, counts, system, asset_counts=None):
    """Build the tag tree, depth first, from names and their item counts.

    Nodes that only exist because a tag below them does are included, so
    ``建筑/红墙`` always has a ``建筑`` row to sit under.  Synonyms are
    folded into one row that lists the other names.

    ``asset_counts`` is how many assets carry each name.  Without it the
    counts are taken to be asset counts, which is what a caller that
    never looks at pages means anyway.
    """
    direct = {}
    assets = {}
    aliases = {}
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        canonical = system.canonical(name)
        direct[canonical] = direct.get(canonical, 0) + counts.get(name, 0)
        source = counts if asset_counts is None else asset_counts
        assets[canonical] = assets.get(canonical, 0) + source.get(name, 0)
        if canonical != name:
            aliases.setdefault(canonical, []).append(name)
    for alias, canonical in system.siblings().items():
        if canonical in direct or alias in names:
            aliases.setdefault(canonical, []).append(alias)

    # Every path a name brings with it, plus the parents it implies, is a
    # row of its own - even when nothing carries that tag directly.
    totals = {}
    for name in list(direct) + [str(n).strip() for n in names]:
        canonical = system.canonical(name)
        if not canonical:
            continue
        for path in _node_paths(canonical, system):
            totals.setdefault(path, 0)
    for name, count in direct.items():
        for path in _node_paths(name, system):
            if path in totals:
                totals[path] += count

    tree = {}
    for name, count in totals.items():
        node = TagNode(name)
        node.count = count
        node.direct = direct.get(name, 0)
        node.asset = assets.get(name, 0)
        node.aliases = sorted(set(aliases.get(name, [])), key=str.casefold)
        tree[name] = node
    roots = []
    for name in sorted(tree, key=str.casefold):
        node = tree[name]
        upper = _tree_parent(name)
        parent = tree.get(upper) if upper else None
        if parent is None:
            roots.append(node)
        else:
            node.parent = parent
            node.depth = parent.depth + 1
            parent.children.append(node)
    ordered = []
    for root in roots:
        _flatten(root, ordered)
    return ordered


def _flatten(node, out):
    out.append(node)
    for child in node.children:
        _flatten(child, out)


def _node_paths(name, system):
    """A tag plus every path level and implied parent above it."""
    paths = [name] + ancestors(name)
    for parent in system.implied_parents(name):
        paths.append(parent)
        paths.extend(ancestors(parent))
    return dict.fromkeys(path for path in paths if path)


def _tree_parent(name):
    """The row a tag hangs under: its path level above it, if any."""
    upper = ancestors(name)
    return upper[0] if upper else None


# ── Editing the tree ──────────────────────────────────────────────────

def rename_path(tag, new_name):
    """The new full name when a tag is renamed or moved.

    ``rename_path('建筑/红墙', '砖墙')`` gives ``建筑/砖墙``;
    ``move_path('建筑/红墙', '材质')`` gives ``材质/红墙``.
    """
    parts = split_path(tag)
    if not parts:
        return new_name
    return join_path(parts[:-1] + [new_name])


def move_path(tag, new_parent):
    """New full name of a tag placed under ``new_parent`` ('' for the root)."""
    return join_path(split_path(new_parent) + [leaf(tag)])


def descendants(names, tag):
    """Every given name that sits below ``tag``."""
    prefix = tag + PATH_SEP
    return [name for name in names if name.startswith(prefix)]


def is_descendant(tag, possible_ancestor):
    """Whether ``tag`` sits below ``possible_ancestor``."""
    return tag.startswith(str(possible_ancestor) + PATH_SEP)


def parse_tree_text(text):
    """Read an indented (or bullet) list of tags from pasted text.

    Both nesting styles work::

        场景
            雨夜
        建筑/红墙
        - 材质
    """
    result = []
    stack = []
    for raw in str(text or '').splitlines():
        line = raw.replace('\t', '    ').rstrip()
        if not line.strip() or line.strip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        name = line.strip().lstrip('-*+•').strip()
        if not name:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parts = [part for _, part in stack]
        parts.extend(split_path(name))
        full = join_path(parts)
        if full and full not in result:
            result.append(full)
        stack.append((indent, name))
    return result


def tree_text(names):
    """The tag catalogue as an indented list, readable by :func:`parse_tree_text`.

    Every level of a path is written out, so a child never ends up nested
    under an unrelated tag when the text is read back.
    """
    paths = {}
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        for path in [name] + ancestors(name):
            paths[path] = len(split_path(path)) - 1
    return '\n'.join('    ' * paths[path] + leaf(path)
                     for path in sorted(
                         paths,
                         key=lambda p: [seg.casefold() for seg in split_path(p)]))
