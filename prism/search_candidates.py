"""Read-only substring candidate queries over a caller-owned compact snapshot.

Two adapters share one word contract.

* The generic adapter reads a ``search_text`` table whose rows carry the
  complete haystack plus an optional FTS5 trigram table. It is the contract
  for a future search-text repository: it never builds or scans a project.
* The catalog adapter reads the project asset catalog cache and shortlists its
  rows through the trigram index that :mod:`prism.project_catalog_index`
  owns. It is what :func:`prism.project_search.search_catalog_assets` runs, so
  the production project search reaches the trigram virtual table whenever the
  catalog has a verified one, and falls back to the exact scan when it has
  not.

Expected columns in the text table: rowid, result_id, haystack. ``haystack`` is
the complete existing search contract's text, casefolded by Python. An optional
FTS5 trigram table uses matching rowids and ``trigram_projection(haystack)``.
NUL must be replaced in the FTS projection because SQLite's tokenizer stops at
it; the exact haystack keeps NUL unchanged. No original assets are stored here.

The trigram index is a *pruner*, never an answer. The indexed path and the
exact path apply the same predicate, so both accept exactly the same rows:
the index only decides which rows are looked at, the exact word filter decides
which rows are published. A word the trigram tokenizer cannot index - one or
two characters - is filtered with the same ``instr(prism_casefold(...))``
expression the exact scan uses, and a query whose words are all that short
keeps the exact scan. There is no prefix index: the contract is a substring
match, and a prefix index would drop interior hits.

Returned IDs are candidates, not ranked UI results. Consume all batches, apply
the existing scope/tag/deletion rules, hydrate exact match fields, and retain
the existing ranking, occurrence positions and result versions. Do not apply a
top-N limit before that ranking. Short queries without an indexable trigram
still scan the compact text table: this is not a constant-time search claim.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3


class SearchCancelled(RuntimeError):
    """An interrupted candidate scan must not be published as complete hits."""


def normalize_haystack(text):
    return text.casefold()


def trigram_projection(normalized_haystack):
    return normalized_haystack.replace('\x00', ' ')


def query_words(query, *, text_filter_enabled=True):
    """casefold + whitespace words, deduplicated, in input order.

    The one AND-token extraction every path shares, so a short / 中文 / 符号 /
    symbol query and a case-insensitive query behave identically whether the
    candidate scan runs through FTS, ``instr`` or the exact Python filter.
    """
    return tuple(dict.fromkeys(query.casefold().split())) if text_filter_enabled else ()


def matches_words(haystack, words):
    """Every AND word is a literal casefolded substring of ``haystack``.

    ``haystack`` must already be casefolded (see :func:`normalize_haystack`),
    matching the ``search_text`` row contract. 中文/短词/符号 stay literal
    substrings; case differences are resolved by the caller's casefold.
    """
    return all(word in haystack for word in words)


#: The tokenizer of the trigram index. ``case_sensitive 1`` keeps the
#: tokenizer from folding a second time: the text it indexes and the query it
#: matches are already casefolded with Python's ``str.casefold()``, so a
#: second folding could only merge two words the exact scan keeps apart.
TRIGRAM_TOKENIZER = 'trigram case_sensitive 1'


@lru_cache(maxsize=1)
def trigram_available():
    """Probe the actual linked SQLite; FTS5 alone does not imply trigram."""
    connection = sqlite3.connect(':memory:')
    try:
        # CREATE VIRTUAL TABLE does not accept bound parameters; the tokenizer
        # is a module constant, never caller input.
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text,tokenize='%s')"
                           % TRIGRAM_TOKENIZER)
        connection.execute('INSERT INTO probe VALUES(?)', ('mixed中文材质',))
        return connection.execute('SELECT rowid FROM probe WHERE probe MATCH ?',
                                  ('"中文材"',)).fetchone() == (1,)
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


@dataclass(frozen=True)
class SubstringPlan:
    words: tuple
    fts_expression: str | None


def substring_plan(query, *, trigram=False, text_filter_enabled=True):
    """Preserve Python casefold + whitespace words + literal substring AND.

    The caller explicitly disables ordinary text filtering where the existing
    global tag-expression branch overrides it. The asset catalog instead ANDs
    ordinary words with tags and must leave text filtering enabled.
    """
    words = query_words(query, text_filter_enabled=text_filter_enabled)
    # One longest anchor suffices to reject impossible rows. Cap its length so
    # pasted text cannot construct an unbounded FTS expression. Exact filters
    # below still check every complete word, including short ones.
    anchor = max(words, key=len, default='') if trigram else ''
    expression = ('"'+trigram_projection(anchor[:64]).replace('"', '""')+'"'
                  if len(anchor) >= 3 else None)
    return SubstringPlan(words, expression)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Expected a simple SQLite table identifier')
    return '"'+value+'"'


def iter_candidate_ids(connection, query, *, table='search_text', fts_table=None,
                       text_filter_enabled=True, batch_size=128, cancel=None):
    """Read one SQLite statement/snapshot, fetching bounded result batches.

    An FTS table is opt-in only after its owner verifies complete, synchronized
    indexing. If this SQLite lacks trigram, exact instr filters remain usable.
    Missing/stale tables are errors, never silently treated as an empty index.
    Cooperative cancellation checks are between batches; callers own SQLite's
    progress handler/worker deadline for a long-running native query.
    """
    if not 1 <= batch_size <= 1024:
        raise ValueError('batch_size must be between 1 and 1024')
    table_name = _identifier(table)
    fts_name = _identifier(fts_table) if fts_table is not None else None
    plan = substring_plan(query, trigram=fts_name is not None and trigram_available(),
                          text_filter_enabled=text_filter_enabled)
    conditions, parameters = [], []
    if plan.fts_expression:
        conditions.append(f'd.rowid IN (SELECT rowid FROM {fts_name} WHERE {fts_name} MATCH ?)')
        parameters.append(plan.fts_expression)
    # SQLite has expression/parameter limits. A very long pasted query must
    # preserve the existing semantics instead of failing at the 1000th word.
    sql_words, remaining_words = plan.words[:64], plan.words[64:]
    for word in sql_words:
        conditions.append('instr(d.haystack, ?) > 0')
        parameters.append(word)
    where = ' WHERE '+' AND '.join(conditions) if conditions else ''
    if cancel is not None and cancel():
        raise SearchCancelled('Candidate query cancelled')
    fields = 'd.result_id,d.haystack' if remaining_words else 'd.result_id'
    cursor = connection.execute(f'SELECT {fields} FROM {table_name} AS d{where} ORDER BY d.rowid',
                                parameters)
    try:
        while True:
            if cancel is not None and cancel():
                raise SearchCancelled('Candidate query cancelled')
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield from (row[0] for row in rows if not remaining_words
                        or all(word in row[1] for word in remaining_words))
    finally:
        cursor.close()


# ── project catalog trigram adapter ───────────────────────────────

#: Columns one catalog candidate row carries. The catalog schema belongs to
#: :mod:`prism.project_catalog_index`; this tuple is the read contract of the
#: candidate scan.
CATALOG_COLUMNS = ('id', 'kind', 'canvas_id', 'title', 'group_id', 'width', 'height',
                   'has_preview', 'has_original', 'tags', 'rating', 'notes', 'filename',
                   'deleted_at')

#: Canonical name of the trigram virtual table over the catalog rows.
CATALOG_FTS_TABLE = 'assets_fts'

#: ``catalog_state`` key that carries the trigram index freshness marker.
TRIGRAM_MARKER_KEY = 'trigram'

#: Escape character of the defensive LIKE helper. The catalog search matches
#: with ``instr``, so ``%`` and ``_`` are literal already; this keeps the
#: same contract available for a path that has to use LIKE.
LIKE_ESCAPE = '\\'


def like_literal(value):
    """Escape a user string so that SQL LIKE matches it literally.

    ``%`` and ``_`` are LIKE wildcards, but a user who types them means those
    exact characters. Use the result together with ``ESCAPE '\\'``.
    """
    text = str('' if value is None else value)
    return (text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
                .replace('%', LIKE_ESCAPE + '%')
                .replace('_', LIKE_ESCAPE + '_'))


def catalog_tag_names(raw):
    """Catalog tags column -> list of tag name strings; never raises."""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if isinstance(value, list):
        return [str(name) for name in value]
    return []


def catalog_haystack(title, filename, notes, tag_names):
    """The canonical casefolded search text of one catalog asset row.

    This is the text the trigram index has to project (see
    :func:`trigram_projection`), so the candidate scan and the exact filter
    agree on what a word may match. The contract for the index owner:

    * the same four columns the catalog's exact scan searches - title,
      filename, notes and the tag names - and nothing else. A page's
      resource-tree path is deliberately *not* part of it, because the
      catalog does not store the path either;
    * casefolded with Python's ``str.casefold()``, so ``Ä``/``ä`` and
      ``Σ``/``σ`` match while ``İ``/``i``/``ı`` stay distinguishable;
    * the four parts joined by one space. A search word never contains a
      space, so a word can only match inside one part - the joined text and
      the four separate texts accept exactly the same words.
    """
    parts = [str(title or ''), str(filename or ''), str(notes or ''),
             ' '.join(str(name) for name in tag_names or ())]
    return normalize_haystack(' '.join(part for part in parts if part))


def catalog_row_haystack(row):
    """``catalog_haystack`` for one row mapping of the catalog table."""
    return catalog_haystack(_row_value(row, 'title'), _row_value(row, 'filename'),
                            _row_value(row, 'notes'),
                            catalog_tag_names(_row_value(row, 'tags', None)))


def _row_value(row, key, default=''):
    """One column of a dict or sqlite3.Row, without assuming a mapping API."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def open_readonly_cache(path):
    """Open a catalog cache read-only; the caller owns the connection.

    The catalog's case-insensitive text helpers are registered on the
    connection, so a statement may filter with the same
    ``instr(prism_casefold(x),?)`` shape the exact scan uses.
    """
    connection = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    return register_catalog_functions(connection)


def register_catalog_functions(connection):
    """Register the catalog's case-insensitive text helpers on a connection.

    The exact scan of the catalog filters with ``instr(prism_casefold(x),?)``
    over title, notes, filename and the tag text: SQLite's ``lower()`` is
    ASCII only and ``LIKE`` would treat ``%`` and ``_`` as wildcards, so
    neither can be used for the search text. The indexed path reuses the
    same filter for the words the trigram tokenizer cannot index, so both
    paths accept exactly the same rows.
    """
    connection.create_function('prism_casefold', 1,
                               lambda value: str(value or '').casefold(),
                               deterministic=True)
    connection.create_function('prism_tag_text', 1,
                               lambda value: ' '.join(catalog_tag_names(value)).casefold(),
                               deterministic=True)
    return connection


def _state_value(connection, key):
    try:
        row = connection.execute('SELECT value FROM catalog_state WHERE key=?',
                                 (key,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row else None


def build_state(connection):
    """The catalog's current build state, or ``None``."""
    value = _state_value(connection, 'build')
    if value is None:
        return None
    try:
        state = json.loads(value)
    except (ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def read_trigram_marker(connection):
    """The trigram freshness marker of a catalog cache, or ``None``."""
    value = _state_value(connection, TRIGRAM_MARKER_KEY)
    if value is None:
        return None
    try:
        marker = json.loads(value)
    except (ValueError, TypeError):
        return None
    return marker if isinstance(marker, dict) else None


def write_trigram_marker(connection, state):
    """Record that the trigram index matches the given catalog build state.

    The index owner calls this in the same transaction that populates the
    virtual table. A consumer compares the marker with the catalog's current
    build state; a marker that no longer matches means the index does not
    describe this catalog, so the consumer falls back to the exact scan
    instead of publishing a short answer.

    ``state`` is the catalog's build state (see :func:`build_state`). The
    marker keeps the three fields that identify the build - ``total``,
    ``last_id`` and ``source`` - plus the indexed table name and the number of
    indexed rows, so a consumer can tell a complete index from a partial one
    without counting the table itself.
    """
    if not isinstance(state, dict):
        raise ValueError('The catalog build state must be a mapping')
    marker = {'total': int(state.get('total') or 0),
              'last_id': state.get('last_id'),
              'source': state.get('source')}
    connection.execute('INSERT OR REPLACE INTO catalog_state(key,value) VALUES(?,?)',
                       (TRIGRAM_MARKER_KEY,
                        json.dumps(marker, ensure_ascii=False, separators=(',', ':'))))
    return marker


@dataclass(frozen=True)
class TrigramIndexStatus:
    """Whether a trigram index may be trusted to shortlist catalog rows."""

    usable: bool
    fts_table: str | None = None
    reason: str = ''
    rows: int | None = None


def _trigram_tables(connection):
    """Every FTS5 trigram virtual table of a database, with its statement."""
    try:
        rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND sql LIKE '%fts5%' AND sql LIKE '%trigram%'").fetchall()
    except sqlite3.DatabaseError:
        return []
    return [(row[0], row[1] or '') for row in rows]


def _table_statement(connection, name):
    """The ``CREATE`` statement of a table, or ``None``."""
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row else None


def _uses_case_sensitive(statement):
    """Whether a table statement declares the case-sensitive trigram tokenizer."""
    statement = (statement or '').lower()
    if 'case_sensitive' not in statement:
        return False
    return statement.split('case_sensitive')[-1].strip().startswith('1')


def _table_columns(connection, table):
    try:
        rows = connection.execute('PRAGMA table_info("%s")'
                                  % table.replace('"', '""')).fetchall()
    except sqlite3.DatabaseError:
        return ()
    return tuple(row[1] for row in rows)


def trigram_index_status(connection, *, table='assets', id_column='id',
                         fts_table=None, columns=CATALOG_COLUMNS, sample=6):
    """Check whether a trigram index over the catalog rows may be used.

    Five things have to hold before a candidate scan may trust the index:

    1. the linked SQLite really has the trigram tokenizer;
    2. the freshness marker matches the catalog's current build state, so the
       index describes *this* catalog and not an older one;
    3. the virtual table is an FTS5 trigram table with one row per catalog
       row, keyed by the catalog id;
    4. the virtual table uses the case-sensitive trigram tokenizer, so the
       index answers exactly what the Python casefolded text answers;
    5. the projected text of sampled rows still carries the catalog text of
       those rows.

    Every failure is reported as unusable: the caller then runs the exact
    scan instead of publishing a short answer.
    """
    if not trigram_available():
        return TrigramIndexStatus(False, None, 'linked sqlite has no trigram tokenizer')
    present = _table_columns(connection, table)
    if not present:
        return TrigramIndexStatus(False, None, 'catalog table is missing')
    missing = [name for name in columns if name not in present]
    if missing:
        return TrigramIndexStatus(False, None,
                                  'catalog table misses columns: '+', '.join(missing))
    state = build_state(connection)
    marker = read_trigram_marker(connection)
    if state is None or marker is None:
        return TrigramIndexStatus(False, None, 'trigram freshness marker is missing')
    if (marker.get('total') != state.get('total')
            or marker.get('last_id') != state.get('last_id')
            or marker.get('source') != state.get('source')):
        return TrigramIndexStatus(False, None,
                                  'trigram index does not match the catalog build state')
    tables = dict(_trigram_tables(connection))
    if fts_table is not None and fts_table in tables:
        name = fts_table
    elif fts_table is None and len(tables) == 1:
        name = next(iter(tables))
    else:
        name = None
    if name is None:
        return TrigramIndexStatus(False, None, 'no trigram index in the catalog cache')
    statement = tables.get(name, '')
    if not _uses_case_sensitive(statement):
        return TrigramIndexStatus(
            False, None,
            'trigram index does not use the case-sensitive trigram tokenizer; '
            'rebuild it with create_trigram_index')
    try:
        indexed = connection.execute('SELECT count(*) FROM "%s"' % name).fetchone()[0]
        total = connection.execute('SELECT count(*) FROM "%s"' % table).fetchone()[0]
        # A partially populated index would answer with a *short* list, which
        # is the one wrong answer: the count has to be exact, not sampled.
        if indexed != total:
            return TrigramIndexStatus(
                False, None,
                'trigram index holds %d of %d catalog rows' % (indexed, total))
        probe = connection.execute('SELECT rowid FROM "%s" ORDER BY rowid LIMIT ?'
                                   % name, (sample,)).fetchall()
        probe += connection.execute('SELECT rowid FROM "%s" ORDER BY rowid DESC LIMIT ?'
                                    % name, (sample,)).fetchall()
        for rowid, in probe:
            record = connection.execute(
                'SELECT title,filename,notes,tags FROM "%s" WHERE %s=?'
                % (table, id_column), (rowid,)).fetchone()
            if record is None:
                return TrigramIndexStatus(False, None,
                                          'trigram index holds an unknown row id')
            projected = connection.execute('SELECT text FROM "%s" WHERE rowid=?' % name,
                                           (rowid,)).fetchone()
            if projected is None:
                return TrigramIndexStatus(False, None, 'trigram index lost a row')
            haystack = str(projected[0] or '').casefold()
            values = [record[0], record[1], record[2]] + catalog_tag_names(record[3])
            for value in values:
                value = str(value or '').casefold()
                if value and value not in haystack:
                    return TrigramIndexStatus(
                        False, None, 'trigram index text does not match the catalog row')
    except sqlite3.DatabaseError as error:
        return TrigramIndexStatus(False, None, 'trigram index is unreadable: %s' % error)
    return TrigramIndexStatus(True, name, 'trigram index verified', total)


@lru_cache(maxsize=8)
def _verified_status(path, size, mtime_ns, table, id_column, fts_table):
    try:
        connection = open_readonly_cache(path)
    except (sqlite3.Error, OSError, ValueError):
        return TrigramIndexStatus(False, None, 'catalog cache is not readable')
    try:
        return trigram_index_status(connection, table=table, id_column=id_column,
                                    fts_table=fts_table)
    except sqlite3.Error:
        return TrigramIndexStatus(False, None, 'catalog cache is not readable')
    finally:
        connection.close()


def cached_trigram_status(cache_path, *, table='assets', id_column='id', fts_table=None):
    """The status of one catalog cache file, re-verified when the file changes.

    Verifying costs a count and a few sampled rows, so the result is cached per
    file identity: a rebuilt catalog changes size or mtime and is verified
    again before it is trusted.
    """
    try:
        stat = os.stat(cache_path)
    except OSError:
        return TrigramIndexStatus(False, None, 'catalog cache file is missing')
    return _verified_status(str(cache_path), stat.st_size, stat.st_mtime_ns,
                             table, id_column, fts_table)


#: How many words one FTS expression may AND together. A longer query is
#: pruned by its longest words first; the remaining words are still checked
#: exactly by the caller, so the cap only limits how much the index prunes.
MAX_FTS_PHRASES = 8

#: The longest phrase one FTS expression may use. The trigram tokenizer turns
#: a phrase into one token per character, so a long phrase buys nothing and
#: costs a long token chain.
MAX_PHRASE_CHARS = 64

#: How many virtual machine instructions run between two cancellation checks
#: of the native statement. Small enough to interrupt a long scan quickly,
#: large enough not to dominate the query.
PROGRESS_TICKS = 10000


def catalog_trigram_plan(query, *, text_filter_enabled=True):
    """The trigram plan of one catalog query, or ``None`` when it has no anchor.

    The trigram tokenizer cannot index a word shorter than three characters,
    so a query whose words are all shorter than that has no indexable anchor.
    Such a query keeps the exact scan; it is never answered by a prefix
    index, because the search contract is a substring match and a prefix
    index would drop interior hits.

    Every word of at least three characters becomes one phrase of the FTS
    expression, AND-ed together. A word is a substring of the row's catalog
    text if and only if it is a substring of one of its parts, so AND-ing the
    phrases prunes the rows down to the ones that hold *every* indexable word
    - the index prunes exactly like the exact scan does. The words that the
    tokenizer cannot express (one or two characters) stay the caller's job.
    """
    words = query_words(query, text_filter_enabled=text_filter_enabled)
    if not words:
        return None
    indexable = [word for word in words if len(word) >= 3]
    if not indexable:
        return None
    # The longest words first: a longer phrase is normally the rarer one.
    phrases = sorted(indexable, key=len, reverse=True)[:MAX_FTS_PHRASES]
    expression = ' AND '.join(
        '"'+trigram_projection(word[:MAX_PHRASE_CHARS]).replace('"', '""')+'"'
        for word in phrases)
    return SubstringPlan(words, expression)


def catalog_candidate_sql(query, *, table='assets', id_column='id', fts_table=None,
                          columns=CATALOG_COLUMNS):
    """The statement the catalog candidate scan runs, and its parameters.

    The statement is split out of :func:`iter_catalog_candidate_rows` so the
    production SQL can be inspected and its query plan checked.
    """
    plan = catalog_trigram_plan(query)
    if plan is None:
        raise ValueError('This query has no indexable trigram anchor')
    name = _identifier(fts_table or CATALOG_FTS_TABLE).strip('"')
    source = _identifier(table).strip('"')
    key = _identifier(id_column).strip('"')
    fields = ', '.join('d."%s"' % column.replace('"', '""') for column in columns)
    conditions, parameters = [], []
    for word in (word for word in plan.words if len(word) < 3):
        conditions.append('(instr(prism_casefold(d."title"),?)>0 OR '
                          'instr(prism_casefold(d."notes"),?)>0 OR '
                          'instr(prism_casefold(d."filename"),?)>0 OR '
                          'instr(prism_tag_text(d."tags"),?)>0)')
        parameters.extend([word] * 4)
    where = (' AND '+' AND '.join(conditions)) if conditions else ''
    sql = ('SELECT {fields} FROM "{source}" AS d '
           'WHERE d."{key}" IN (SELECT rowid FROM "{name}" WHERE "{name}" MATCH ?) '
           'AND d."deleted_at" IS NULL '
           "AND d.\"kind\" NOT IN ('text','path'){where} ORDER BY d.\"{key}\"").format(
               fields=fields, source=source, name=name, key=key, where=where)
    return sql, (plan.fts_expression, *parameters)


def iter_catalog_candidate_rows(connection, query, *, table='assets', id_column='id',
                                fts_table=None, columns=CATALOG_COLUMNS,
                                batch_size=256, cancel=None):
    """Yield the catalog rows that can match ``query``, shortlisted by trigram.

    The trigram index only rejects rows: the caller still has to apply the
    exact word filter, so a candidate is never a promise. The scan runs one
    statement and reads it in bounded batches, so the index is evaluated once
    for the whole scan instead of once per keyset page - re-evaluating it per
    page costs an order of magnitude more than the whole scan. A cancelled
    scan raises :class:`SearchCancelled` instead of returning a short answer.

    The FTS lookup is a subquery, not a join. A join made SQLite materialise
    every match into a temporary B-tree to sort it (``USE TEMP B-TREE FOR
    ORDER BY``); the subquery only materialises the matching row ids and lets
    the primary key lookups produce the rows in id order without a sort.

    The words the trigram tokenizer cannot index - one or two characters -
    are filtered with the same ``instr(prism_casefold(...))`` predicate the
    exact scan uses, so the indexed path accepts exactly the rows the exact
    scan accepts: neither path can publish a row the other one drops.

    ``query`` needs an indexable anchor; :func:`catalog_trigram_plan` decides
    that before this is called.
    """
    if not 1 <= batch_size <= 1024:
        raise ValueError('batch_size must be between 1 and 1024')
    sql, parameters = catalog_candidate_sql(
        query, table=table, id_column=id_column, fts_table=fts_table, columns=columns)
    register_catalog_functions(connection)
    if cancel is not None and cancel():
        raise SearchCancelled('Catalog candidate query cancelled')
    cursor = None
    # The native statement runs outside the batch loop, so a cancel that
    # arrives while SQLite is still scanning has to interrupt it there.
    handler = None
    if cancel is not None and hasattr(connection, 'set_progress_handler'):
        def handler():
            return 1 if cancel() else 0
        connection.set_progress_handler(handler, PROGRESS_TICKS)
    try:
        cursor = connection.execute(sql, parameters)
        names = [description[0] for description in cursor.description]
        while True:
            if cancel is not None and cancel():
                raise SearchCancelled('Catalog candidate query cancelled')
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield dict(zip(names, row))
    except sqlite3.OperationalError as error:
        if (cancel is not None and cancel()
                and getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT):
            raise SearchCancelled('Catalog candidate query cancelled') from None
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if handler is not None:
            connection.set_progress_handler(None, 0)


def create_trigram_index(connection, fts_table=CATALOG_FTS_TABLE):
    """Create the trigram virtual table of the catalog cache, if it is missing.

    The index owner calls this once per catalog build; the projection has to
    be :func:`trigram_projection` of :func:`catalog_haystack` for every
    catalog row, with the virtual table rowid equal to the catalog id.

    The table uses the case-sensitive trigram tokenizer: the indexed text and
    the query are casefolded with Python before they reach SQLite, so the
    tokenizer must not fold a second time. A table with another tokenizer is
    refused by :func:`trigram_index_status`, which sends the consumer back to
    the exact scan.

    Idempotent: an existing table with the right tokenizer is left alone. An
    existing table with *another* tokenizer is dropped and created again,
    because the index is derived data and the caller populates it right after
    this call. It creates an *empty* table;
    :func:`populate_trigram_index` fills it.
    """
    name = _identifier(fts_table).strip('"')
    statement = _table_statement(connection, name)
    if statement and not _uses_case_sensitive(statement):
        connection.execute('DROP TABLE IF EXISTS "%s"' % name)
    # CREATE VIRTUAL TABLE does not accept bound parameters; the tokenizer is
    # a module constant, never caller input.
    connection.execute('CREATE VIRTUAL TABLE IF NOT EXISTS "%s" '
                       "USING fts5(text, tokenize='%s')" % (name, TRIGRAM_TOKENIZER))
    return name


def populate_trigram_index(connection, fts_table, rows):
    """Fill the trigram virtual table from ``(catalog id, haystack)`` pairs.

    The content is **replaced**: the table is emptied first, so calling it
    twice with the same rows leaves the same index. The caller owns the
    transaction: the index and the catalog rows have to become visible
    together, otherwise a reader could see one without the other.

    ``rows`` are ``(catalog id, haystack)`` pairs. The haystack is projected
    with :func:`trigram_projection` of :func:`normalize_haystack` here, so the
    caller may pass the raw :func:`catalog_haystack` text.
    """
    name = _identifier(fts_table).strip('"')
    connection.execute('DELETE FROM "%s"' % name)
    connection.executemany('INSERT INTO "%s"(rowid,text) VALUES(?,?)' % name,
                           ((rowid, trigram_projection(normalize_haystack(haystack)))
                            for rowid, haystack in rows))
    return name
