#!/usr/bin/env python3
"""
topology.py — Bare-bones distribution feeder connectivity.

Mainline only. No impedance, no laterals, no fuses, no load flow. The single
job is to say which devices are electrically upstream of which, and which
feeders can back each other up through a normally-open tie. That is enough to
answer the questions event files alone cannot:

  * two records within a few hundred ms — same fault seen at two depths, or
    two unrelated faults?  (are the devices on one path to the source?)
  * a device locked out — what is downstream of it, and is there a tie that
    could pick that section up?
  * a device recorded a fault but did not trip — was the device that *did*
    trip below it, which is coordination working, or beside it, which is not?

The format is deliberately hand-authorable. Real feeder connectivity gets
typed into a spreadsheet from a wall map, not exported from GIS, so the file
is one row per node and five columns:

    feeder,node_id,kind,parent,tie_to

  feeder   the feeder this node belongs to; the substation name on a source row
  node_id  matches device_id in devices.csv for anything that records events
  kind     source | breaker | recloser | sectionalizer | pmh | tie
  parent   the node immediately upstream; empty on a source row
  tie_to   the far-end node, on a tie row only
  model    PMH-9 / PMH-11 / PMH-10 on a pmh row; how many ways the cabinet has

Each protective device owns the section immediately downstream of it, so a
feeder is a source row, a head device, a handful of mid-line reclosers and its
ties — six or seven lines. Normally-closed edges form one tree per source;
`kind=tie` rows are exactly the normally-open edges, and closing one re-parents
a subtree onto another feeder.

Customers are NOT carried here. They live in devices.csv (`customers_served`)
so there is one source of truth; `customers_below()` sums the subtree from the
registry.

Usage
-----
  python3 -m comtrade_analyzer.topology demo/topology.csv
  python3 -m comtrade_analyzer.topology demo/topology.csv --devices demo/devices.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

# Shared with the device registry so a node_id and a device_id that differ only
# in punctuation still join. Imported rather than re-implemented: a second copy
# is how the diagnostics channel check drifted away from the analysis it was
# meant to mirror.
from .wso_impact import _normalize

SOURCE_KINDS = ("source",)
TIE_KINDS = ("tie",)
SWITCHING_KINDS = ("breaker", "recloser", "sectionalizer", "pmh")
VALID_KINDS = SOURCE_KINDS + SWITCHING_KINDS + TIE_KINDS

# Automatic pad-mounted gear. A cabinet's WAYS are its edges in this model:
# the source way is its parent, each load way a child, and a normally-open way
# to another feeder is a tie anchored to it. Only automatic cabinets are
# mapped, and only switch ways — fused ways are not connectivity anyone
# switches, so they stay off the drawing like every other fuse.
PMH_WAYS = {"PMH-9": 2, "PMH-11": 3, "PMH-10": 4}

# Kinds expected to appear in devices.csv and produce COMTRADE records. A tie
# with a recloser control does record — that is how a FLISR restoration shows
# up — so presence in the registry, not kind, is the real test. This is only
# the default expectation used to pick the level of a validation finding.
#
# A PMH way switch is a load-interrupter, not a protective device: it does not
# clear faults and so leaves no oscillography. It switches, so it is connectivity
# and it matters for N-1 — but expecting records from it would be wrong.
RECORDING_KINDS = ("breaker", "recloser")

_HEADER = ("feeder", "node_id", "kind", "parent", "tie_to", "model")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """One mainline node. `parent` and `tie_to` hold raw ids as authored."""
    node_id: str
    feeder: str
    kind: str
    parent: str = ""
    tie_to: str = ""
    model: str = ""             # PMH-9 / PMH-11 / PMH-10 on a pmh row
    row: int = 0                # source line number, for error messages

    @property
    def is_source(self) -> bool:
        return self.kind in SOURCE_KINDS

    @property
    def is_tie(self) -> bool:
        return self.kind in TIE_KINDS


class Network:
    """
    A loaded topology. Lookups are punctuation- and case-insensitive; every
    method takes and returns ids as they were authored.
    """

    def __init__(self, nodes: Iterable[Node]):
        self._nodes: Dict[str, Node] = {}
        self._order: List[str] = []
        # Rows dropped because an earlier row already claimed the id. Kept so
        # validate() can report them — the graph itself only sees the first.
        self.dropped: List[Node] = []
        for n in nodes:
            key = _normalize(n.node_id)
            if not key:
                continue
            if key in self._nodes:
                self.dropped.append(n)
                continue
            self._nodes[key] = n
            self._order.append(key)
        self._kids: Dict[str, List[str]] = {k: [] for k in self._nodes}
        for key in self._order:
            pkey = _normalize(self._nodes[key].parent)
            if pkey and pkey in self._kids:
                self._kids[pkey].append(key)

    # -- basics ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: str) -> bool:
        return _normalize(node_id) in self._nodes

    def node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(_normalize(node_id))

    def nodes(self) -> List[Node]:
        """Every node, in file order."""
        return [self._nodes[k] for k in self._order]

    def sources(self) -> List[Node]:
        return [n for n in self.nodes() if n.is_source]

    def ties(self, feeder: str = "") -> List[Node]:
        """Normally-open tie rows, optionally only those touching one feeder."""
        out = [n for n in self.nodes() if n.is_tie]
        if not feeder:
            return out
        keep = []
        for t in out:
            far = self.node(t.tie_to)
            if t.feeder == feeder or (far is not None and far.feeder == feeder):
                keep.append(t)
        return keep

    def feeders(self) -> List[str]:
        """Feeder names, in file order. Source rows carry a station, not a feeder."""
        seen, out = set(), []
        for n in self.nodes():
            if n.is_source or not n.feeder or n.feeder in seen:
                continue
            seen.add(n.feeder)
            out.append(n.feeder)
        return out

    def devices(self, feeder: str = "") -> List[Node]:
        """Switching devices — the things that can open and can hold a record."""
        return [n for n in self.nodes()
                if n.kind in SWITCHING_KINDS and (not feeder or n.feeder == feeder)]

    # -- graph -------------------------------------------------------------

    def parent_of(self, node_id: str) -> Optional[Node]:
        n = self.node(node_id)
        return self.node(n.parent) if n and n.parent else None

    def children(self, node_id: str) -> List[Node]:
        """Nodes directly downstream. Tie rows hang off their parent like any child."""
        return [self._nodes[k] for k in self._kids.get(_normalize(node_id), [])]

    def path_to_source(self, node_id: str) -> List[Node]:
        """
        The node, then everything upstream of it, ending at the source.
        Returns [] for an unknown id and stops short if the chain is broken or
        loops — validate() is what reports why.
        """
        out: List[Node] = []
        seen: Set[str] = set()
        cur = self.node(node_id)
        while cur is not None:
            key = _normalize(cur.node_id)
            if key in seen:
                break
            seen.add(key)
            out.append(cur)
            if cur.is_source or not cur.parent:
                break
            cur = self.node(cur.parent)
        return out

    def subtree(self, node_id: str, cross_ties: bool = False) -> List[Node]:
        """
        The node and everything downstream of it — what goes dark if it opens.
        Ties are included as leaves but are not traversed through unless
        `cross_ties`, because a normally-open tie carries nothing.
        """
        start = self.node(node_id)
        if start is None:
            return []
        out: List[Node] = []
        seen: Set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            key = _normalize(cur.node_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(cur)
            if cur.is_tie and not cross_ties:
                continue
            stack.extend(reversed(self.children(cur.node_id)))
            if cur.is_tie and cross_ties:
                far = self.node(cur.tie_to)
                if far is not None:
                    stack.append(far)
        return out

    def depth(self, node_id: str) -> int:
        """Hops from the source. -1 if the node is unknown."""
        path = self.path_to_source(node_id)
        return len(path) - 1 if path else -1

    def is_upstream_of(self, upper: str, lower: str) -> bool:
        """True if `upper` lies on `lower`'s path to the source (and is not it)."""
        ku, kl = _normalize(upper), _normalize(lower)
        if not ku or ku == kl:
            return False
        return any(_normalize(n.node_id) == ku
                   for n in self.path_to_source(lower)[1:])

    def on_same_path(self, node_ids: Iterable[str]) -> bool:
        """
        True if every id lies on one radial path to the source — i.e. they all
        saw the same fault current. Two devices on sibling branches do not.
        """
        ids = [i for i in node_ids if i in self]
        if len(ids) < 2:
            return True
        deepest = self.deepest(ids)
        if deepest is None:
            return False
        path = {_normalize(n.node_id) for n in self.path_to_source(deepest.node_id)}
        return all(_normalize(i) in path for i in ids)

    def deepest(self, node_ids: Iterable[str]) -> Optional[Node]:
        """The furthest node from the source — the one that should have cleared."""
        known = [self.node(i) for i in node_ids]
        known = [n for n in known if n is not None]
        if not known:
            return None
        return max(known, key=lambda n: self.depth(n.node_id))

    def customers_below(self, node_id: str, registry: dict) -> int:
        """
        Customers on this node and everything downstream, summed out of
        devices.csv. Nodes absent from the registry contribute nothing.
        """
        total = 0
        for n in self.subtree(node_id):
            dev = registry.get(_normalize(n.node_id))
            if dev:
                total += int(dev.get("customers_served", 0) or 0)
        return total

    def backup_ties(self, node_id: str) -> List[Node]:
        """Tie rows sitting inside the subtree below `node_id` — the ways back in."""
        return [n for n in self.subtree(node_id) if n.is_tie]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_topology(csv_path: str) -> Network:
    """
    Read a topology CSV. Unknown columns are ignored; missing optional columns
    are treated as empty, so a file with only the four columns a radial feeder
    needs still loads.
    """
    nodes: List[Node] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in raw.items()}
            # A comment is any row whose FIRST cell starts with '#'. Checking
            # only node_id misses a comment containing a comma, which the CSV
            # reader then splits into columns — and people write commas.
            if (row.get("feeder", "").startswith("#")
                    or row.get("node_id", "").startswith("#")):
                continue
            nid = row.get("node_id", "")
            if not nid:
                continue
            nodes.append(Node(
                node_id=nid,
                feeder=row.get("feeder", ""),
                kind=row.get("kind", "").lower(),
                parent=row.get("parent", ""),
                tie_to=row.get("tie_to", ""),
                model=row.get("model", ""),
                row=i,
            ))
    return Network(nodes)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _finding(level, code, message, detail="", fix=""):
    """Same shape as diagnostics.py — symptom, evidence, fix."""
    return {"level": level, "code": code, "message": message,
            "detail": detail, "fix": fix}


_LEVEL_RANK = {"error": 0, "warning": 1, "info": 2}


def validate(net: Network, registry: Optional[dict] = None) -> List[dict]:
    """
    Check a topology for the mistakes hand-authoring actually produces, worst
    first. Every finding names the row so it can be fixed in the spreadsheet.
    """
    out: List[dict] = []

    for dup in net.dropped:
        first = net.node(dup.node_id)
        out.append(_finding(
            "error", "duplicate_node",
            f"node_id {dup.node_id!r} appears more than once",
            f"rows {first.row if first else '?'} and {dup.row}; only the first is loaded",
            "Node ids must be unique; rename one or delete the duplicate row."))

    for n in net.nodes():
        if n.kind not in VALID_KINDS:
            out.append(_finding(
                "error", "unknown_kind",
                f"{n.node_id}: kind {n.kind!r} is not recognised",
                f"row {n.row}",
                f"Use one of: {', '.join(VALID_KINDS)}."))

        if n.is_source:
            if n.parent:
                out.append(_finding(
                    "error", "source_has_parent",
                    f"{n.node_id}: a source row must have an empty parent",
                    f"row {n.row}, parent={n.parent!r}",
                    "Clear the parent column — the source is the top of the tree."))
        elif not n.parent:
            out.append(_finding(
                "error", "no_parent",
                f"{n.node_id}: no parent, and it is not a source",
                f"row {n.row}",
                "Name the node immediately upstream, or set kind=source."))
        elif n.parent not in net:
            out.append(_finding(
                "error", "missing_parent",
                f"{n.node_id}: parent {n.parent!r} is not a node in this file",
                f"row {n.row}",
                "Check the spelling, or add the missing upstream row."))

        if n.is_tie:
            if not n.tie_to:
                out.append(_finding(
                    "error", "tie_without_far_end",
                    f"{n.node_id}: tie row has no tie_to",
                    f"row {n.row}",
                    "Name the device on the far side of the tie."))
            elif n.tie_to not in net:
                out.append(_finding(
                    "error", "missing_tie_far_end",
                    f"{n.node_id}: tie_to {n.tie_to!r} is not a node in this file",
                    f"row {n.row}",
                    "Check the spelling, or add the far-end device row."))
            else:
                far = net.node(n.tie_to)
                if far.feeder and n.feeder and far.feeder == n.feeder:
                    out.append(_finding(
                        "warning", "tie_within_one_feeder",
                        f"{n.node_id}: both ends are on {n.feeder}",
                        f"row {n.row}, tie_to={n.tie_to}",
                        "A tie normally joins two feeders. If this is a mid-line "
                        "loop switch that is fine — otherwise fix tie_to."))
        elif n.tie_to:
            out.append(_finding(
                "warning", "tie_to_on_non_tie",
                f"{n.node_id}: tie_to is set but kind is {n.kind!r}",
                f"row {n.row}",
                "tie_to is only read on kind=tie rows; set kind=tie or clear it."))

    # A cabinet cannot have more ways connected than it has.
    for n in net.nodes():
        if n.kind != "pmh":
            if n.model:
                out.append(_finding(
                    "warning", "model_on_non_cabinet",
                    f"{n.node_id}: model {n.model!r} is set but kind is {n.kind!r}",
                    f"row {n.row}",
                    "model is only read on kind=pmh rows; clear it or set kind=pmh."))
            continue
        if not n.model:
            out.append(_finding(
                "warning", "cabinet_without_model",
                f"{n.node_id}: no model, so its way count cannot be checked",
                f"row {n.row}",
                f"Set model to one of: {', '.join(PMH_WAYS)}."))
            continue
        if n.model not in PMH_WAYS:
            out.append(_finding(
                "error", "unknown_cabinet_model",
                f"{n.node_id}: {n.model!r} is not a cabinet model this knows",
                f"row {n.row}",
                f"Use one of: {', '.join(f'{m} ({w} ways)' for m, w in PMH_WAYS.items())}."))
            continue
        used = (1 if n.parent else 0) + len(net.children(n.node_id))
        if used > PMH_WAYS[n.model]:
            out.append(_finding(
                "error", "cabinet_over_connected",
                f"{n.node_id}: {used} ways connected, {n.model} has "
                f"{PMH_WAYS[n.model]}",
                f"row {n.row}",
                "Count the source way, every load way and any normally-open "
                "way to another feeder. Fused ways are not mapped."))

    # Reciprocal ties — the same pair authored from both sides.
    pairs: Dict[frozenset, Node] = {}
    for t in net.ties():
        if not t.tie_to or t.tie_to not in net:
            continue
        pair = frozenset((_normalize(t.parent), _normalize(t.tie_to)))
        if pair in pairs:
            out.append(_finding(
                "warning", "duplicate_tie",
                f"{t.node_id}: the same tie is already authored as {pairs[pair].node_id}",
                f"rows {pairs[pair].row} and {t.row}",
                "Author each tie once, from either side; the model is undirected."))
        pairs.setdefault(pair, t)

    # Every node must reach exactly one source.
    for n in net.nodes():
        if n.is_source:
            continue
        path = net.path_to_source(n.node_id)
        if not path:
            continue
        top = path[-1]
        if _normalize(top.node_id) == _normalize(n.node_id) and len(path) > 1:
            continue
        if not top.is_source:
            if _normalize(top.node_id) == _normalize(n.node_id) and not top.parent:
                continue        # already reported as no_parent
            if top.parent and top.parent in net:
                out.append(_finding(
                    "error", "loop",
                    f"{n.node_id}: its upstream path loops and never reaches a source",
                    f"row {n.row}, stopped at {top.node_id}",
                    "A radial feeder is a tree. Break the cycle in the parent column."))
            elif top.parent:
                pass        # already reported as missing_parent
            else:
                out.append(_finding(
                    "error", "orphan_branch",
                    f"{n.node_id}: reaches {top.node_id}, which has no source above it",
                    f"row {n.row}",
                    "Give the top of this branch kind=source, or a parent."))

    if not net.sources():
        out.append(_finding(
            "error", "no_source",
            "no source row in this file",
            f"{len(net)} node(s) loaded",
            "Add one row per substation bus with kind=source and an empty parent."))

    for feeder in net.feeders():
        heads = [d for d in net.devices(feeder)
                 if (p := net.parent_of(d.node_id)) is not None and p.is_source]
        if not heads:
            out.append(_finding(
                "warning", "feeder_without_head",
                f"{feeder}: no device connects directly to a source",
                "",
                "Every feeder should start at a substation breaker whose parent "
                "is the bus."))
        elif len(heads) > 1:
            out.append(_finding(
                "info", "feeder_with_several_heads",
                f"{feeder}: {len(heads)} devices connect straight to a source",
                ", ".join(h.node_id for h in heads),
                "Normal for a feeder fed from two buses; check it is intended."))
        if not net.ties(feeder):
            out.append(_finding(
                "info", "feeder_without_tie",
                f"{feeder}: no tie to any other feeder",
                "",
                "Nothing can back this feeder up. Fine if that is true on the "
                "ground — otherwise a tie row is missing."))

    if registry is not None:
        for n in net.nodes():
            if n.kind not in RECORDING_KINDS:
                continue
            if _normalize(n.node_id) not in registry:
                out.append(_finding(
                    "info", "device_not_in_registry",
                    f"{n.node_id}: in the topology but not in the device registry",
                    f"row {n.row}",
                    "Add it to devices.csv to give it a zone, risk tier and "
                    "customer count. Until then it has no events and no "
                    "customers attached."))
        # Feeder names have to agree, not just device ids. The dashboard keys
        # its per-feeder totals by the feeder in the event header and labels
        # its dropdown from this file; a mismatch splits the page in half,
        # narrowing the panels that count events and not the ones that read a
        # total.
        reg_feeders = {d.get("feeder", "").strip().lower()
                       for d in registry.values() if d.get("feeder")}
        if reg_feeders:
            for feeder in net.feeders():
                if feeder.strip().lower() not in reg_feeders:
                    out.append(_finding(
                        "warning", "feeder_name_mismatch",
                        f"feeder {feeder!r} is not a feeder name in the registry",
                        ", ".join(sorted(reg_feeders)[:4]) + " ...",
                        "The dashboard groups per-feeder totals by the feeder "
                        "name in the event header, and labels its picker from "
                        "this file. Spell them the same or the two disagree."))

        for key, dev in registry.items():
            if key not in net:
                out.append(_finding(
                    "info", "device_not_in_topology",
                    f"{dev.get('device_id', key)}: in the device registry but "
                    "not in the topology",
                    dev.get("feeder", ""),
                    "Add a row for it so its events can be placed on a feeder."))

    out.sort(key=lambda f: _LEVEL_RANK.get(f["level"], 3))
    return out


# ---------------------------------------------------------------------------
# ASCII single-line
# ---------------------------------------------------------------------------

_GLYPH = {
    "source":       "═",
    "breaker":      "▮",
    "recloser":     "◍",
    "sectionalizer": "◌",
    "pmh":          "▣",
    "tie":          "○",
}

_ID_COL = 46      # left column width for the id, so the kinds line up


def single_line(net: Network, feeder: str = "", registry: Optional[dict] = None) -> str:
    """
    The network as an indented single-line sketch, one block per source.
    Pass `feeder` to draw only the paths that touch one feeder.
    """
    keep: Optional[Set[str]] = None
    if feeder:
        keep = set()
        for n in net.nodes():
            if n.feeder != feeder:
                continue
            for anc in net.path_to_source(n.node_id):
                keep.add(_normalize(anc.node_id))
        # A tie into this feeder may be authored from the far side, so pull in
        # the path above *both* ends — otherwise the tie is kept but hangs off
        # a parent that was never drawn, and silently disappears.
        for t in net.ties(feeder):
            keep.add(_normalize(t.node_id))
            for end in (t.parent, t.tie_to):
                for anc in net.path_to_source(end):
                    keep.add(_normalize(anc.node_id))

    lines: List[str] = []

    def tail(n: Node) -> str:
        """Everything right of the id column."""
        bits = [n.kind + (f" {n.model}" if n.model else "")]
        if n.is_tie:
            far = net.node(n.tie_to)
            far_txt = far.node_id if far else f"{n.tie_to} (unknown)"
            far_feeder = f" \u00b7 {far.feeder}" if far and far.feeder else ""
            bits.append(f"N.O. \u2543\u2192 {far_txt}{far_feeder}".replace("\u2543", "\u254c\u254c"))
        elif n.feeder and not n.is_source:
            bits.append(n.feeder)
        if registry is not None and not n.is_source:
            cust = net.customers_below(n.node_id, registry)
            if cust:
                bits.append(f"{cust:,} cust below")
        return "  ".join(bits)

    def walk(n: Node, prefix: str, last: bool, top: bool = False) -> None:
        if keep is not None and _normalize(n.node_id) not in keep:
            return
        connector = "" if top else ("\u2514\u2500 " if last else "\u251c\u2500 ")
        left = f"{prefix}{connector}{_GLYPH.get(n.kind, '\u00b7')} {n.node_id}"
        lines.append(f"{left:<{_ID_COL}}{tail(n)}".rstrip())
        child_prefix = "" if top else prefix + ("   " if last else "\u2502  ")
        kids = [c for c in net.children(n.node_id)
                if keep is None or _normalize(c.node_id) in keep]
        for i, c in enumerate(kids):
            walk(c, child_prefix, i == len(kids) - 1)

    for src_node in net.sources():
        if keep is not None and _normalize(src_node.node_id) not in keep:
            continue
        if lines:
            lines.append("")
        header = src_node.feeder or src_node.node_id
        lines.append(header)
        lines.append("\u2500" * max(len(header), 8))
        walk(src_node, "", True, top=True)

    return "\n".join(lines)


def summary(net: Network) -> str:
    """One line of counts, for the head of a report."""
    devs = net.devices()
    return (f"{len(net.sources())} substation(s), {len(net.feeders())} feeder(s), "
            f"{len(devs)} device(s), {len(net.ties())} tie(s)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Load, validate and draw a mainline feeder topology.")
    p.add_argument("topology", help="Topology CSV (feeder,node_id,kind,parent,tie_to)")
    p.add_argument("--devices", metavar="CSV",
                   help="Device registry, to cross-check ids and total customers")
    p.add_argument("--feeder", metavar="NAME", default="",
                   help="Draw only the paths touching this feeder")
    p.add_argument("--quiet", action="store_true",
                   help="Validation findings only, no diagram")
    args = p.parse_args(argv)

    if not os.path.exists(args.topology):
        print(f"error: no such file: {args.topology}", file=sys.stderr)
        return 2

    net = load_topology(args.topology)
    registry = None
    if args.devices:
        from .wso_impact import load_registry
        registry = load_registry(args.devices)

    print(summary(net))
    if registry is not None:
        print(f"Registry : {len(registry)} device(s) from {args.devices}")
    print()

    if not args.quiet:
        print(single_line(net, args.feeder, registry))
        print()

    findings = validate(net, registry)
    errors = [f for f in findings if f["level"] == "error"]
    if not findings:
        print("Validation: clean.")
        return 0

    print(f"Validation: {len(errors)} error(s), "
          f"{sum(1 for f in findings if f['level'] == 'warning')} warning(s), "
          f"{sum(1 for f in findings if f['level'] == 'info')} note(s)")
    for f in findings:
        print(f"  [{f['level'].upper():7}] {f['code']}: {f['message']}")
        if f["detail"]:
            print(f"             {f['detail']}")
        if f["fix"]:
            print(f"             fix: {f['fix']}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
