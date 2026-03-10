# bgpsim

BGP path propagation inference. Given an AS-level topology with annotated
business relationships, bgpsim computes all AS-paths tied for best towards a
set of prefix origins, following the Gao-Rexford routing policy model.

## Dependencies

- Python 3.10+
- [NetworkX](https://networkx.org/)

## Quick start

```python
from bgpsim import ASGraph, Announcement, Relationship

# Build a small topology:
#   1
#  / \
# 2   3
#  \ /
#   4
graph = ASGraph()
graph.add_peering(1, 2, Relationship.P2C)
graph.add_peering(1, 3, Relationship.P2C)
graph.add_peering(2, 4, Relationship.P2C)
graph.add_peering(3, 4, Relationship.P2C)

# AS 1 announces a prefix to all neighbors.
announce = Announcement.make_anycast_announcement(graph, [1])
graph.infer_paths(announce)

# AS 4 learns two equally-preferred paths through its providers:
print(graph.g.nodes[4]["best-paths"])  # [(2, 1), (3, 1)]
print(graph.g.nodes[4]["path-pref"])   # PathPref.PROVIDER (1)
```

## API reference

### Types

```python
ASPath = tuple[int, ...]
ImportFilter = Callable[[int, list[ASPath], Any], list[ASPath]]
```

`ASPath` is a tuple of AS numbers representing a route. The first element is
the next-hop AS and the last element is the origin. `ImportFilter` is the
signature for custom import filter functions (see
[`ASGraph.set_import_filter`](#asgraphset_import_filter)).

### `Relationship`

An `IntEnum` encoding the business relationship on a directed edge. The value
on the edge `(A, B)` describes A's role relative to B:

| Member | Value | Meaning                                                 |
|--------|-------|---------------------------------------------------------|
| `P2C`  | -1    | A is a **provider** of B (A provides transit to B)      |
| `P2P`  | 0     | A and B are **peers** (settlement-free interconnection) |
| `C2P`  | 1     | A is a **customer** of B (A purchases transit from B)   |

`Relationship.reversed()` returns the relationship as seen from the other
end of the edge (e.g. `P2C.reversed() == C2P`).

### `PathPref`

An `IntEnum` modelling route preference at the importing AS.  Higher values
mean higher preference, matching the Gao-Rexford "prefer customer" rule:

| Member     | Value | When used                     |
|------------|-------|-------------------------------|
| `CUSTOMER` | 3     | Route learned from a customer |
| `PEER`     | 2     | Route learned from a peer     |
| `PROVIDER` | 1     | Route learned from a provider |
| `UNKNOWN`  | 0     | No route learned yet          |

`PathPref.from_relationship(graph, exporter, importer)` derives the
preference the importer would assign to a route received from the exporter,
based on the edge relationship in the graph.

### `ASGraph`

The central class. Wraps a NetworkX `DiGraph` and provides methods to build
the topology, configure policies, and run the inference algorithm.

#### `ASGraph.add_peering(source, sink, relationship)`

Add a bidirectional peering link. `relationship` is interpreted from
`source`'s perspective (e.g. `Relationship.P2C` means `source` is a
provider of `sink`). The reverse edge is added automatically. Duplicate
edges with the same relationship are silently ignored; duplicate edges with
different relationships raise `ValueError`.

```python
graph = ASGraph()
graph.add_peering(1, 2, Relationship.P2C)   # 1 is provider of 2
graph.add_peering(3, 4, Relationship.P2P)   # 3 and 4 are peers
graph.add_peering(5, 6, Relationship.C2P)   # 5 is customer of 6
```

#### `ASGraph.set_import_filter(asn, func, data=None)`

Attach a custom import filter to an AS. The filter is called whenever the AS
is about to import routes from a neighbor. It receives the exporter's ASN,
the candidate paths (each already prepended with the exporter), and the
optional `data` argument. It must return the subset of paths to accept.

```python
def only_accept_origin(exporter, paths, allowed_origin):
    return [p for p in paths if p[-1] == allowed_origin]

graph.set_import_filter(42, only_accept_origin, 100)
```

This can be used to implement mechanisms like peer-locking or selective
route filtering.

#### `ASGraph.infer_paths(announce)`

Run the path inference algorithm for the given `Announcement`. This performs
a modified breadth-first search that processes edges in decreasing order of
relationship preference (customer > peer > provider), and within the same
preference, in increasing order of path length. The result is that every AS
in the graph ends up with all AS-paths tied for best.

After calling this method, per-node results are available on the underlying
NetworkX graph:

| Node attribute | Type           | Description                                             |
|----------------|----------------|---------------------------------------------------------|
| `"best-paths"` | `list[ASPath]` | All AS-paths tied for best at this node                 |
| `"path-pref"`  | `PathPref`     | Preference of the best paths (`UNKNOWN` if unreachable) |
| `"path-len"`   | `int`          | Length of the best paths                                |

```python
announce = Announcement.make_anycast_announcement(graph, [origin_asn])
graph.infer_paths(announce)

for node in graph.g.nodes:
    paths = graph.g.nodes[node]["best-paths"]
    pref = graph.g.nodes[node]["path-pref"]
    print(f"AS{node}: {len(paths)} path(s), pref={pref.name}")
```

**Important:** `infer_paths` can only be called once per `ASGraph` instance
because it mutates node metadata. Use `ASGraph.clone()` to run multiple
inferences on the same topology.

#### `ASGraph.clone()`

Return a deep copy of the graph (topology, node attributes, tier-1 and IXP
sets). The clone has no announcement state, so `infer_paths` can be called
on it. Import filters and callbacks are **not** copied.

```python
base = ASGraph()
# ... build topology ...
g1 = base.clone()
g1.infer_paths(announce_a)

g2 = base.clone()
g2.infer_paths(announce_b)
```

#### `ASGraph.set_callback(when, func)`

Register a callback for observing the inference algorithm. See
[`InferenceCallback`](#inferencecallback) for available hooks.

#### `ASGraph.read_caida_asrel_graph(filepath)`

Static method. Load a [CAIDA AS-relationship][caida-asrel] dataset (bz2
compressed). Returns a fully constructed `ASGraph` with `tier1s` and `ixps`
populated from the file's metadata comments.

```python
graph = ASGraph.read_caida_asrel_graph("20200101.as-rel.txt.bz2")
print(f"{len(graph.g.nodes)} ASes, {len(graph.g.edges)//2} peerings")
print(f"Tier-1 ASes: {graph.tier1s}")
```

### `Announcement`

Specifies which ASes originate a prefix and what AS-path they announce to
each neighbor. The core data structure is `source2neighbor2path`: a nested
dict mapping each source AS to a dict of neighbor AS to the AS-path
prepended to the announcement towards that neighbor.

#### `Announcement.make_anycast_announcement(asgraph, sources)`

Convenience constructor. Creates an announcement where each source
advertises to all its neighbors with an empty AS-path (no prepending).
`sources` can be a `list[int]` (no prepending) or a `dict[int, int]`
mapping each source to a prepend count.

```python
# Single origin, no prepending:
announce = Announcement.make_anycast_announcement(graph, [100])

# Anycast from two origins:
announce = Announcement.make_anycast_announcement(graph, [100, 200])

# AS 100 prepends once (appears twice in the path):
announce = Announcement.make_anycast_announcement(graph, {100: 1})
```

#### Custom announcements

For fine-grained control (per-neighbor prepending, poisoning), construct an
`Announcement` directly:

```python
announce = Announcement(source2neighbor2path={
    100: {
        200: (),        # normal announcement to AS 200
        300: (100,),    # prepend once towards AS 300
        400: (100, 100),  # prepend twice towards AS 400
    }
})
```

### `InferenceCallback`

An enum of hooks that can be registered via `ASGraph.set_callback`:

| Member                     | Called when                                            | Signature                                                                |
|----------------------------|--------------------------------------------------------|--------------------------------------------------------------------------|
| `START_RELATIONSHIP_PHASE` | Algorithm starts processing a relationship type        | `(pref: Relationship) -> None`                                           |
| `NEIGHBOR_ANNOUNCE`        | Origin announces to a neighbor at the start of a phase | `(origin: int, neighbor: int, pref: Relationship, path: ASPath) -> None` |
| `VISIT_EDGE`               | Algorithm processes an edge during BFS                 | `(exporter: int, importer: int, pref: Relationship) -> None`             |

## Running the tests

We have some tests to check the propagation algorithm in pre-built topologies.  Run with:

```bash
python3 -m unittest tests/test_bgpsim.py
```

## Basic benchmarking

You can check the runtime of random path inferences on CAIDA's January 2020 graph by using the `tests/bench_bgpsim.py` script. It reports 5 averages over 32 full inference runs each. Disabling assertions with `-O` makes the code significantly faster as it is pretty heavy on asserts.

```bash
$ python3 tests/bench_bgpsim.py
[1065.9921099510975, 1129.7197931839619, 1341.9510222299723, 1212.2649150219513, 1117.318360270001]
$ python3 -O tests/bench_bgpsim.py
[244.108187089907, 246.5742465169169, 244.00426896300633, 235.13479439693037, 255.45060764998198]
```

## References

You may want to check these papers on an [introduction to BGP routing policies][bgp-policies], and on [how policies can be inferred in the wild][caida-asrel].

## TO-DO

* Write tests for poisoned announcements. The code should work for announcements with poisoning, but there are no tests for this functionality yet.

[bgp-policies]: https://doi.org/10.1109/MNET.2005.1541715
[caida-asrel]: https://doi.org/10.1145/2504730.2504735
