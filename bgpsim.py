from __future__ import annotations

import bz2
import copy
import dataclasses
import enum
import logging
import os
from collections import Counter, defaultdict
from typing import Any, Callable

import networkx as nx

ASPath = tuple[int, ...]
ImportFilter = Callable[[int, list[ASPath], Any], list[ASPath]]

NODE_PATH_PREF = "path-pref"
NODE_BEST_PATHS = "best-paths"
NODE_PATH_LEN = "path-len"
NODE_IMPORT_FILTER = "import-filter"
NODE_HAS_PROVIDER = "has-provider"
EDGE_REL = "edge-attr-relationship"


class PathPref(enum.IntEnum):
    """Model preference of paths imported by an AS.

    >>> assert PathPref.CUSTOMER > PathPref.PEER
    >>> assert PathPref.PEER > PathPref.PROVIDER
    """

    CUSTOMER = 3
    PEER = 2
    PROVIDER = 1
    UNKNOWN = 0

    @staticmethod
    def from_relationship(graph: ASGraph, exporter: int, importer: int) -> PathPref:
        """Compute the PathPref at importer given the relationship in the ASGraph."""
        rel = graph.g[importer][exporter][EDGE_REL]
        if rel == Relationship.P2C:
            return PathPref.CUSTOMER
        if rel == Relationship.P2P:
            return PathPref.PEER
        if rel == Relationship.C2P:
            return PathPref.PROVIDER
        raise ValueError(f"Unsupported relationship {rel}")


class Relationship(enum.IntEnum):
    """Model the peering relationship between a pair of ASes.

    Use less-than comparisons to mean better-than:
    >>> assert Relationship.P2C < Relationship.P2P
    >>> assert Relationship.P2P < Relationship.C2P
    """

    C2P = 1
    P2P = 0
    P2C = -1

    def reversed(self) -> Relationship:
        """Get Relationship in the opposite direction of an edge.

        >>> assert Relationship.P2C == Relationship.C2P.reversed()
        >>> assert Relationship.P2P == Relationship.P2P.reversed()
        """
        return Relationship(-1 * self.value)


class InferenceCallback(enum.Enum):
    """Callback hooks available in the inference algorithm

    START_RELATIONSHIP_PHASE is called whenever the algorithm starts
    processing relationships of a given type (class Relationship).
    Relationships are processed in order of preference (P2C->P2P->C2P).
    Example signature:

    def callback(pref: Relationship) -> None

    VISIT_EDGE is called whenever the algorithm processes an edge.  Some
    edges are not "visited" in the inference process: (i) Edges where
    the AS importing routes is announcing the prefix; (ii) Edges visited
    when the origin makes the first announcement to a neighbor (see
    NeighborAnnounceCallback).  Example signature:

    def callback(exporter: int, importer: int, pref: Relationship) -> None

    NeighborAnnounceCallback is called whenever we start a new phase and
    initialize routes at neighbors of origins.  Example signature:

    def callback(origin: int, neighbor: int, pref: Relationship, path: ASPath) -> None
    """

    START_RELATIONSHIP_PHASE = "start-relationship-phase"
    NEIGHBOR_ANNOUNCE = "neighbor-announce"
    VISIT_EDGE = "visit-edge"


@dataclasses.dataclass
class Announcement:
    """Specification of a prefix announcement.

    A prefix can be announced simulataneously by a set of source ASes.
    Each source AS can announce the prefix to all or a subset of its
    neighbors. Towards each neighbor, a source can manipulate the
    AS-path on its announcement, e.g., to perform AS-path prepending or
    AS-path poisoning.
    """

    source2neighbor2path: dict[int, dict[int, ASPath]]

    @staticmethod
    def make_anycast_announcement(
        asgraph: ASGraph, sources: list[int] | dict[int, int]
    ) -> Announcement:
        """Make announcement from sources to all neighbors without prepending."""
        src2nei2path: dict[int, dict[int, ASPath]] = {}
        if isinstance(sources, list):
            sources = {asn: 0 for asn in sources}
        for src in sources:
            src2nei2path[src] = {nei: () for nei in asgraph.g[src]}
        return Announcement(src2nei2path)


class WorkQueue:
    def __init__(self):
        self.pref2depth2edge = {
            PathPref.CUSTOMER: defaultdict(list),
            PathPref.PEER: defaultdict(list),
            PathPref.PROVIDER: defaultdict(list),
        }

    def get(self, pref: PathPref) -> tuple[int, int] | None:
        """Get the edge exporting the shortest paths with pref."""
        if not self.pref2depth2edge[pref]:
            return None
        depth = min(self.pref2depth2edge[pref])
        edge = self.pref2depth2edge[pref][depth].pop()
        if not self.pref2depth2edge[pref][depth]:
            del self.pref2depth2edge[pref][depth]
        return edge

    def add_work(self, graph: ASGraph, exporter: int) -> None:
        """Add work to forward paths at importer to downstream ASes"""
        pref = graph.g.nodes[exporter][NODE_PATH_PREF]
        for downstream in graph.g[exporter]:
            downstream_pref = PathPref.from_relationship(graph, exporter, downstream)
            if pref == PathPref.CUSTOMER or downstream_pref == PathPref.PROVIDER:
                depth = graph.g.nodes[exporter][NODE_PATH_LEN]
                edge = (exporter, downstream)
                self.pref2depth2edge[downstream_pref][depth].append(edge)

    def check_work(self, graph: ASGraph, exporter: int) -> bool:
        """Check all neighbors importing from exporter are in work queue"""
        pref = graph.g.nodes[exporter][NODE_PATH_PREF]
        for downstream in graph.g[exporter]:
            downstream_pref = PathPref.from_relationship(graph, exporter, downstream)
            if pref == PathPref.CUSTOMER or downstream_pref == PathPref.PROVIDER:
                depth = graph.g.nodes[exporter][NODE_PATH_LEN]
                edge = (exporter, downstream)
                assert edge in self.pref2depth2edge[downstream_pref][depth]
        return True


class ASGraph:
    def __init__(self):
        self.g = nx.DiGraph()
        self.workqueue = WorkQueue()
        self.announce: Announcement | None = None
        self.callbacks: dict[InferenceCallback, Callable] = {}
        self.tier1s: set[int] = set()
        self.ixps: set[int] = set()

    def add_peering(self, source: int, sink: int, relationship: Relationship) -> None:
        """Add nodes and edges corresponding to a peering relationship."""
        assert source != sink
        data = self.g.get_edge_data(source, sink, None)
        if data is not None:
            if data[EDGE_REL] != relationship:
                raise ValueError("Duplicate edges with different relationships")
            return
        if source not in self.g:
            self.g.add_node(source)
            self.g.nodes[source][NODE_BEST_PATHS] = []
            self.g.nodes[source][NODE_PATH_PREF] = PathPref.UNKNOWN
            self.g.nodes[source][NODE_IMPORT_FILTER] = None
            self.g.nodes[source][NODE_HAS_PROVIDER] = False
        if sink not in self.g:
            self.g.add_node(sink)
            self.g.nodes[sink][NODE_BEST_PATHS] = []
            self.g.nodes[sink][NODE_PATH_PREF] = PathPref.UNKNOWN
            self.g.nodes[sink][NODE_IMPORT_FILTER] = None
            self.g.nodes[sink][NODE_HAS_PROVIDER] = False
        self.g.add_edge(source, sink)
        self.g[source][sink][EDGE_REL] = Relationship(relationship)
        self.g.add_edge(sink, source)
        self.g[sink][source][EDGE_REL] = relationship.reversed()
        if relationship == Relationship.C2P:
            self.g.nodes[source][NODE_HAS_PROVIDER] = True
        elif relationship == Relationship.P2C:
            self.g.nodes[sink][NODE_HAS_PROVIDER] = True

    def set_import_filter(self, asn: int, func: ImportFilter, data: Any = None) -> None:
        """Set import filter for an AS.

        The filter function receives the exporter ASN and the exported
        AS-paths tied for best. The exported AS-paths already include
        the exporter's ASN. It should return the set of AS-paths that
        are actually imported (not discarded). The data variable will be
        passed to the filter function.

        filter(exporter: int, paths: list[ASPath], data) -> list[ASPath]
        """
        self.g.nodes[asn][NODE_IMPORT_FILTER] = (func, data)

    def set_callback(self, when: InferenceCallback, func: Callable) -> None:
        self.callbacks[when] = func

    def check_announcement(self, announce: Announcement) -> None:
        """Check all relationships exist and that there are no bogus poisonings."""
        for source, neighbor2path in announce.source2neighbor2path.items():
            if source not in self.g:
                raise ValueError(f"Source AS{source} not in ASGraph")
            for neigh, path in neighbor2path.items():
                if neigh not in self.g[source]:
                    raise ValueError(f"Peering AS{source}-AS{neigh} not in ASGraph")
                if neigh in path:
                    raise ValueError(f"Neighbor AS{neigh} poisoned in announcement from AS{source} with path {path}")

    def infer_paths(self, announce: Announcement):
        """Infer all AS-paths tied for best toward announcement sources.

        This function performs a modified breadth-first search traversing peering links
        in decreasing order of relationship preference. An AS that has learned a path
        with preference X will never choose paths with preference worse than X nor
        longer paths with preference equal to X. These two properties, combined, allow
        us to compute the best paths directly, without ever generating less preferred or
        longer paths that would eventually be replaced by the best paths.

        A path that traverses a P2P or a C2P link can only be learned through providers.
        After we have processed all routes learnable from (indirect) customers (and only
        customers), there is no need to ever revisit customer routes. ASes choosing
        between multiple provider routes only care about AS-path length: They do not
        care about whether the provider routes traverse a P2P or any number of C2P
        links.

        This function can only be called once, as it adds metadata to self.g nodes and
        edges. To infer AS-paths for multiple announcements, consider cloning the graph
        with ASGraph.clone().
        """

        assert self.announce is None
        self.check_announcement(announce)
        self.announce = announce

        for pref in [PathPref.CUSTOMER, PathPref.PEER, PathPref.PROVIDER]:
            if InferenceCallback.START_RELATIONSHIP_PHASE in self.callbacks:
                self.callbacks[InferenceCallback.START_RELATIONSHIP_PHASE](pref)
            self._make_announcements(pref)
            edge = self.workqueue.get(pref)
            while edge:
                exporter, importer = edge
                if InferenceCallback.VISIT_EDGE in self.callbacks:
                    self.callbacks[InferenceCallback.VISIT_EDGE](
                        exporter, importer, pref
                    )
                if importer in announce.source2neighbor2path:
                    # Do not import route at sources.
                    edge = self.workqueue.get(pref)
                    continue
                assert PathPref.from_relationship(self, exporter, importer) == pref
                if self._update_paths(exporter, importer):
                    self.workqueue.add_work(self, importer)
                edge = self.workqueue.get(pref)

    def _make_announcements(self, pref: PathPref) -> None:
        """Initialize paths with given pref at neighbors according to announcement."""

        assert self.announce is not None
        # We sort the calls to update_paths() by path length as update_paths() does not
        # allow paths to get shorter due to the breadth-first search.
        nei2len2srcs: dict[int, dict[int, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for src, nei2aspath in self.announce.source2neighbor2path.items():
            for nei, aspath in nei2aspath.items():
                if PathPref.from_relationship(self, src, nei) != pref:
                    continue
                if InferenceCallback.NEIGHBOR_ANNOUNCE in self.callbacks:
                    announce_path = self.announce.source2neighbor2path[src][nei]
                    self.callbacks[InferenceCallback.NEIGHBOR_ANNOUNCE](
                        src, nei, pref, announce_path
                    )
                nei2len2srcs[nei][len(aspath)].append(src)

        for nei, len2srcs in nei2len2srcs.items():
            # We discard all paths longer than the shortest.
            length = min(len2srcs.keys())
            for src in len2srcs[length]:
                announce_path = self.announce.source2neighbor2path[src][nei]
                if self._update_paths(src, nei, announce_path):
                    self.workqueue.add_work(self, nei)

    def _update_paths(
        self, exporter: int, importer: int, announce_path: tuple[int, ...] | None = None
    ) -> bool:
        """Check for new paths or add paths tied for best at importer.

        Returns True if importer just got its first paths (work needs to be enqueued).
        Returns False otherwise, including if importer just learned new paths (in this
        case we check that work is already enqueued).

        The announce_path parameter ignores paths at exporter and allows setting
        arbitrary paths at importer. This is used to handle different announcements to
        different neighbors.
        """
        node = self.g.nodes[importer]
        new_pref = PathPref.from_relationship(self, exporter, importer)
        current_pref = node[NODE_PATH_PREF]

        assert current_pref >= new_pref or current_pref == PathPref.UNKNOWN

        if current_pref > new_pref:
            return False

        new_paths = None
        if announce_path is not None:
            assert importer not in announce_path
            new_paths = [(exporter,) + announce_path]
        else:
            exported_paths = self.g.nodes[exporter][NODE_BEST_PATHS]
            new_paths = [(exporter,) + p for p in exported_paths if importer not in p]

        if node[NODE_IMPORT_FILTER] is not None:
            func, data = node[NODE_IMPORT_FILTER]
            new_paths = func(exporter, new_paths, data)
        if not new_paths:
            return False

        new_path_len = len(new_paths[0])

        if current_pref == PathPref.UNKNOWN:
            self.g.nodes[importer][NODE_BEST_PATHS] = new_paths
            self.g.nodes[importer][NODE_PATH_LEN] = new_path_len
            self.g.nodes[importer][NODE_PATH_PREF] = new_pref
            return True

        current_path_len = self.g.nodes[importer][NODE_PATH_LEN]
        assert current_pref == new_pref
        assert new_path_len >= current_path_len

        if new_path_len == current_path_len:
            self.g.nodes[importer][NODE_BEST_PATHS].extend(new_paths)
            assert self.workqueue.check_work(self, importer)

        return False

    def clone(self) -> ASGraph:
        """Return a deep copy of the current ASGraph."""
        assert self.announce is None
        graph = ASGraph()
        graph.g = copy.deepcopy(self.g)
        graph.workqueue = WorkQueue()
        graph.announce = None
        return graph

    @staticmethod
    def read_caida_asrel_graph(filepath: str | os.PathLike) -> ASGraph:
        def parse_relationship_line(line):
            # <provider-as>|<customer-as>|-1
            # <peer-as>|<peer-as>|0
            source, sink, rel = line.strip().split("|")
            return int(source), int(sink), Relationship(int(rel))

        graph = ASGraph()
        cnt = Counter(lines=0, peerings=0)
        with bz2.open(filepath, "rt") as fd:
            for line in fd:
                cnt["lines"] += 1
                if line[0] == "#":
                    # TODO: store metadata in ASGraph
                    continue
                source, sink, rel = parse_relationship_line(line)
                graph.add_peering(source, sink, rel)
                cnt["peerings"] += 1
        logging.info(
            "read %s: %d lines, %d peering relationships",
            filepath,
            cnt["lines"],
            cnt["peerings"],
        )
        return graph
