// Negotiated-congestion router, second version: rules come from the board.
//
// Compared with negotiate.cpp:
//   - each net has a class (track width, clearance, via size); what a net may
//     enter, where it may drill, and how far it keeps other nets away all
//     depend on its class
//   - 45-degree moves, with no cutting across the corner of a forbidden cell
//   - every connection is written out as an ordered path, so it can be turned
//     back into tracks
//
// Model, as before: nets may share cells at a price that grows each iteration.
// A net's centreline cells keep other nets' centrelines out of a disc whose
// radius depends on both nets' classes. `occupancy[c]` counts, for a net of
// class c looking at a cell, how many nets' discs cover it. A via is a blob of
// centreline cells on every layer.
//
// Costs are integers: `scale` units for a straight step, about 1.41 times that
// for a diagonal one, multiplied by the cell's price and capped at `cap`.
//
// Usage: negotiate2 <board> <label> <scale> <cap> <heap|buckets> [routes-out]

#include <algorithm>
#include <csignal>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <queue>
#include <random>
#include <string>
#include <vector>

using i64 = int64_t;
using u32 = uint32_t;
static const int16_t FREE = -1, NOBODY = -2;

static const int MAX_ITERATIONS = 60;

// SIGTERM or SIGINT: finish the current round, then write the result as it stands.
static volatile std::sig_atomic_t stop_requested = 0;
static void request_stop(int) { stop_requested = 1; }
static const double PRESENT_LIMIT = 1e6;

// What a run may vary: the order nets are routed in and the congestion-price schedule.
// The defaults reproduce negotiate2.
struct Config {
    std::string order = "span_asc";  // span_asc, span_desc, pads_desc, area_desc, random
    unsigned seed = 0;               // for order = random
    double present_start = 0.5, present_growth = 1.6;
    float history_step = 0.5f;
    double via_factor = 1.0;         // multiplies the board's via cost
};

struct Pad {
    std::vector<u32> cells;
    int r0, r1, c0, c1;
};
struct Net {
    int id = 0, cls = 0;
    std::vector<Pad> pads;
    std::vector<u32> path;  // centreline cells and via blobs, off the net's own pads
    std::vector<u32> tree;  // cells the net's copper may be entered from
    std::vector<std::vector<u32>> connections;  // each as cells from the newly joined pad back to the tree
    std::vector<std::vector<u32>> footprint;    // per observing class: cells whose occupancy this net raised
    int via_count = 0;
    bool dead = false;
    int span = 0;
    int blocked_pad = -1;  // a pad no track can reach whatever else is on the board; found once, remembered
    // From a previous session (NEGOTIATE_PRESET): centreline cells and via positions (in-plane) of the
    // old route still standing. Laid without searching on the first routing; given up for good the
    // first time the net is ripped up.
    std::vector<u32> preset, preset_vias;
    bool kept = false;  // the preset is part of the net's current copper
    size_t preset_n = 0;  // path[0, preset_n) came from the preset
    std::vector<std::vector<u32>> preset_footprint;  // per observing class, counted in preset_occ
    int conflict_rounds = 0;  // consecutive rounds in conflict
};

struct Board {
    int layers = 0, rows = 0, cols = 0, classes = 0, via_cost = 0;
    std::vector<double> blob;                      // via blob radius per class, in cells
    std::vector<std::vector<int16_t>> core;        // per class: the net whose pad wholly covers a track centred here
    std::vector<double> exclusion;                 // [a * classes + b] centre-to-centre distance, in cells
    std::vector<std::vector<int16_t>> owner;       // per class: which net alone may enter a cell
    std::vector<std::vector<int16_t>> via_static;  // per class: which net alone may drill here
    std::vector<int16_t> pad_net;
    std::vector<Net> nets;
    size_t plane() const { return size_t(rows) * cols; }
    size_t cells() const { return plane() * layers; }
};

static void check_sites(const Board& b, const std::vector<std::vector<int16_t>>& via);

static bool load(const char* path, Board& b) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    int32_t h[6];
    if (fread(h, 4, 6, f) != 6) return false;
    b.layers = h[0], b.rows = h[1], b.cols = h[2], b.classes = h[3], b.via_cost = h[5];
    const int net_count = h[4], k = b.classes;
    std::vector<int32_t> blob(k), radii(size_t(k) * k);
    bool ok = fread(blob.data(), 4, k, f) == size_t(k) && fread(radii.data(), 4, radii.size(), f) == radii.size();
    for (int32_t r : blob) b.blob.push_back(r / 1000.0);
    for (int32_t r : radii) b.exclusion.push_back(r / 1000.0);
    const size_t n = b.cells(), plane = b.plane();
    std::vector<std::vector<int16_t>> trace(k, std::vector<int16_t>(n)), via(k, std::vector<int16_t>(plane));
    for (auto& map : trace) ok = ok && fread(map.data(), 2, n, f) == n;
    for (auto& map : via) ok = ok && fread(map.data(), 2, plane, f) == plane;
    b.core.assign(k, std::vector<int16_t>(n));
    for (auto& map : b.core) ok = ok && fread(map.data(), 2, n, f) == n;
    b.pad_net.resize(n);
    ok = ok && fread(b.pad_net.data(), 2, n, f) == n;
    // Which net alone may drill each site, the whole via considered: the exporter works it out (and
    // redraws it only around an edit); NEGOTIATE_CHECK_SITES recomputes it here and reports differences.
    b.via_static.assign(k, std::vector<int16_t>(plane));
    for (auto& map : b.via_static) ok = ok && fread(map.data(), 2, plane, f) == plane;
    b.nets.resize(net_count);
    for (Net& net : b.nets) {
        int32_t head[3];
        ok = ok && fread(head, 4, 3, f) == 3;
        net.id = head[0], net.cls = head[1];
        net.pads.resize(head[2]);
        net.footprint.resize(k);
        int r0 = b.rows, r1 = 0, c0 = b.cols, c1 = 0;
        for (Pad& pad : net.pads) {
            int32_t count = 0;
            ok = ok && fread(&count, 4, 1, f) == 1;
            pad.cells.resize(count);
            ok = ok && fread(pad.cells.data(), 4, count, f) == size_t(count);
            pad.r0 = b.rows, pad.r1 = 0, pad.c0 = b.cols, pad.c1 = 0;
            for (u32 cell : pad.cells) {
                int r = int((cell % plane) / b.cols), c = int(cell % b.cols);
                pad.r0 = std::min(pad.r0, r), pad.r1 = std::max(pad.r1, r);
                pad.c0 = std::min(pad.c0, c), pad.c1 = std::max(pad.c1, c);
            }
            r0 = std::min(r0, pad.r0), r1 = std::max(r1, pad.r1), c0 = std::min(c0, pad.c0), c1 = std::max(c1, pad.c1);
        }
        net.span = (r1 - r0) + (c1 - c0);
    }
    fclose(f);
    if (!ok) return false;
    b.owner.resize(k);
    for (int cls = 0; cls < k; ++cls) {
        // A cell nobody may enter is still open to a net whose own pad would wholly
        // cover a track centred there: such a track adds no copper.
        b.owner[cls].resize(n);
        for (size_t i = 0; i < n; ++i)
            b.owner[cls][i] = (trace[cls][i] == NOBODY && b.core[cls][i] >= 0) ? b.core[cls][i] : trace[cls][i];
    }
    if (getenv("NEGOTIATE_CHECK_SITES")) check_sites(b, via);
    return true;
}

static void check_sites(const Board& b, const std::vector<std::vector<int16_t>>& via) {
    const size_t plane = b.plane();
    for (int cls = 0; cls < b.classes; ++cls) {
        // A via needs its whole blob, on every layer, to be enterable by the same net.
        std::vector<int16_t> site(plane, NOBODY);
        const int reach = int(std::floor(b.blob[cls]));
        const double limit = b.blob[cls] * b.blob[cls];
        for (int r = 0; r < b.rows; ++r)
            for (int c = 0; c < b.cols; ++c) {
                int16_t who = via[cls][size_t(r) * b.cols + c];
                for (int dr = -reach; dr <= reach && who != NOBODY; ++dr)
                    for (int dc = -reach; dc <= reach && who != NOBODY; ++dc) {
                        if (dr * dr + dc * dc > limit) continue;
                        int rr = r + dr, cc = c + dc;
                        if (rr < 0 || rr >= b.rows || cc < 0 || cc >= b.cols) {
                            who = NOBODY;
                            break;
                        }
                        for (int l = 0; l < b.layers; ++l) {
                            int16_t here = b.owner[cls][l * plane + size_t(rr) * b.cols + cc];
                            if (here == FREE) continue;
                            who = (who == FREE || who == here) && here != NOBODY ? here : NOBODY;
                        }
                    }
                site[size_t(r) * b.cols + c] = who;
            }
        size_t differ = 0;
        for (size_t i = 0; i < plane; ++i) differ += site[i] != b.via_static[cls][i];
        fprintf(stderr, "sites class %d: %zu cells differ\n", cls, differ);
    }
}

// Working arrays the size of the grid start at zero. Allocated with calloc and never written to
// zero, their pages stay unmapped until the search first touches them: an incremental run, which
// searches a small part of the board, then never pays to clear the rest.
template <class T>
struct ZeroAlloc {
    using value_type = T;
    ZeroAlloc() = default;
    template <class U>
    ZeroAlloc(const ZeroAlloc<U>&) {}
    T* allocate(size_t count) {
        if (void* p = std::calloc(count, sizeof(T))) return static_cast<T*>(p);
        throw std::bad_alloc();
    }
    void deallocate(T* p, size_t) { std::free(p); }
    template <class U>
    void construct(U*) noexcept {}  // value-initialised means zero, which calloc gave
    template <class U, class... Args>
    void construct(U* p, Args&&... args) { ::new (static_cast<void*>(p)) U(std::forward<Args>(args)...); }
    template <class U>
    bool operator==(const ZeroAlloc<U>&) const { return true; }
};
template <class T>
using Zeroed = std::vector<T, ZeroAlloc<T>>;

template <class T>
static std::vector<Zeroed<T>> zeroed(int count, size_t size) {
    std::vector<Zeroed<T>> maps;
    for (int i = 0; i < count; ++i) maps.emplace_back(size);
    return maps;
}

struct Variant {
    i64 scale, cap;
    bool buckets;
    double via_factor = 1.0;
    i64 weight_tenths = 10;  // the search estimate is multiplied by this over ten; above 10 trades path cost for speed
};

struct Router {
    const Board& b;
    Variant v;
    i64 diagonal;
    std::vector<Zeroed<uint16_t>> occupancy;  // per observing class
    // Footprints of copper kept from a previous session, counted apart: two kept pieces were legal
    // together before the edit, so their overlap on this grid (its rounding margin) is no conflict.
    std::vector<Zeroed<uint16_t>> preset_occ;
    Zeroed<u32> conflict_mark;
    u32 conflict_id = 0;
    double kept_price = 5.0;  // NEGOTIATE_KEPT_PRICE: what crossing kept copper costs, per cell, from round 1
    std::vector<Zeroed<u32>> stamped;
    Zeroed<float> history;
    Zeroed<i64> best;
    Zeroed<u32> parent, seen, closed, is_target, in_path;
    u32 search_id = 0, path_id = 0, stamp_id = 0;
    std::vector<std::vector<std::pair<int, int>>> disc;  // [laying class * classes + observing class]
    std::vector<std::vector<std::pair<int, int>>> blob;  // per class
    double present = 0;
    long expansions = 0;
    double search_ns = 0;
    std::vector<std::vector<u32>> levels;
    Zeroed<u32> via_seen;   // per (row, col): the search that last priced a via here
    Zeroed<i64> via_price;

    Router(const Board& board, Variant variant) : b(board), v(variant) {
        const size_t n = b.cells();
        diagonal = std::llround(v.scale * std::sqrt(2.0));
        occupancy = zeroed<uint16_t>(b.classes, n);
        preset_occ = zeroed<uint16_t>(b.classes, n);
        stamped = zeroed<u32>(b.classes, n);
        conflict_mark = Zeroed<u32>(n);
        history = Zeroed<float>(n), best = Zeroed<i64>(n);
        via_seen = Zeroed<u32>(b.plane()), via_price = Zeroed<i64>(b.plane());
        parent = Zeroed<u32>(n), seen = Zeroed<u32>(n), closed = Zeroed<u32>(n), is_target = Zeroed<u32>(n), in_path = Zeroed<u32>(n);
        for (double radius : b.exclusion) {
            std::vector<std::pair<int, int>> offsets;
            int reach = int(std::ceil(radius));
            for (int dr = -reach; dr <= reach; ++dr)
                for (int dc = -reach; dc <= reach; ++dc)
                    if (dr * dr + dc * dc < radius * radius) offsets.push_back({dr, dc});
            disc.push_back(offsets);
        }
        for (double radius : b.blob) {
            std::vector<std::pair<int, int>> offsets;
            const int reach = int(std::floor(radius));
            for (int dr = -reach; dr <= reach; ++dr)
                for (int dc = -reach; dc <= reach; ++dc)
                    if (dr * dr + dc * dc <= radius * radius) offsets.push_back({dr, dc});
            blob.push_back(offsets);
        }
    }

    inline double multiple(u32 cell, int cls) const {
        return (1.0 + history[cell]) * (1.0 + present * occupancy[cls][cell] + kept_price * preset_occ[cls][cell]);
    }
    inline i64 units(double m, i64 base) const { return std::clamp<i64>(std::llround(m * base), base, base * v.cap); }
    inline bool enterable(u32 cell, const Net& net) const {
        const int16_t who = b.owner[net.cls][cell];
        return who == FREE || who == net.id;
    }

    i64 via_units(u32 in_plane, int cls) const {
        double worst = 1.0;
        const int r = int(in_plane / b.cols), c = int(in_plane % b.cols);
        for (auto [dr, dc] : blob[cls])
            for (int l = 0; l < b.layers; ++l)
                worst = std::max(worst, multiple(u32(l * b.plane() + size_t(r + dr) * b.cols + (c + dc)), cls));
        return std::max<i64>(1, std::llround(b.via_cost * v.via_factor)) * units(worst, v.scale);
    }

    void lay_footprint(Net& net) {
        const size_t plane = b.plane();
        if (net.preset_footprint.size() != size_t(b.classes)) net.preset_footprint.resize(b.classes);
        for (int observer = 0; observer < b.classes; ++observer) {
            ++stamp_id;
            const auto& offsets = disc[net.cls * b.classes + observer];
            auto& marks = stamped[observer];
            // New copper first, then kept copper where new copper does not already cover it: each
            // cell counts the net once, in one of the two tallies.
            for (int pass = 0; pass < 2; ++pass) {
                auto& counts = pass == 0 ? occupancy[observer] : preset_occ[observer];
                auto& footprint = pass == 0 ? net.footprint[observer] : net.preset_footprint[observer];
                const size_t from = pass == 0 ? net.preset_n : 0, to = pass == 0 ? net.path.size() : net.preset_n;
                for (size_t i = from; i < to; ++i) {
                    const u32 cell = net.path[i];
                    const u32 in_plane = cell % plane, base = cell - in_plane;
                    const int r = int(in_plane / b.cols), c = int(in_plane % b.cols);
                    for (auto [dr, dc] : offsets) {
                        int rr = r + dr, cc = c + dc;
                        if (rr < 0 || rr >= b.rows || cc < 0 || cc >= b.cols) continue;
                        u32 near = base + u32(rr) * b.cols + cc;
                        if (marks[near] == stamp_id) continue;
                        marks[near] = stamp_id;
                        ++counts[near];
                        footprint.push_back(near);
                    }
                }
            }
        }
    }

    // keep_preset: take up only what was searched; the kept copper is laid again by route().
    void rip_up(Net& net, bool keep_preset = false) {
        for (int observer = 0; observer < b.classes; ++observer) {
            for (u32 cell : net.footprint[observer]) --occupancy[observer][cell];
            net.footprint[observer].clear();
            if (size_t(observer) < net.preset_footprint.size()) {
                for (u32 cell : net.preset_footprint[observer]) --preset_occ[observer][cell];
                net.preset_footprint[observer].clear();
            }
        }
        net.preset_n = 0;
        net.path.clear(), net.tree.clear(), net.connections.clear();
        net.via_count = 0;
        if (!keep_preset) net.preset.clear(), net.preset_vias.clear(), net.kept = false, net.blocked_pad = -1;
    }

    // Whether the net is in conflict; preset_hit says if any conflict lies on its kept copper.
    bool conflicted(const Net& net, bool* preset_hit = nullptr) {
        // Where the net itself is counted at a cell: in occupancy if its new copper covers the cell.
        ++conflict_id;
        bool hit = false;
        if (preset_hit) *preset_hit = false;
        for (u32 cell : net.footprint[net.cls]) conflict_mark[cell] = conflict_id;
        for (size_t i = 0; i < net.path.size(); ++i) {
            const u32 cell = net.path[i];
            const bool own_new = conflict_mark[cell] == conflict_id;
            const int new_others = int(occupancy[net.cls][cell]) - (own_new ? 1 : 0);
            const int kept_others = int(preset_occ[net.cls][cell]) - (own_new ? 0 : 1);
            if (i < net.preset_n ? new_others > 0 : new_others + kept_others > 0) {
                if (!preset_hit) return true;
                if (i < net.preset_n) { *preset_hit = true; return true; }
                hit = true;
            }
        }
        return hit;
    }

    void add_to_path(Net& net, u32 cell) {
        if (b.core[net.cls][cell] == net.id || in_path[cell] == path_id) return;
        in_path[cell] = path_id;
        net.path.push_back(cell);
    }

    bool connect(Net& net, const Pad& target) {
        const size_t plane = b.plane();
        const int cols = b.cols, rows = b.rows;
        ++search_id;
        for (u32 cell : target.cells) is_target[cell] = search_id;
        auto heuristic = [&](u32 cell) -> i64 {
            const u32 in_plane = cell % plane;
            const int r = int(in_plane / cols), c = int(in_plane % cols);
            const i64 dr = std::max(0, std::max(target.r0 - r, r - target.r1));
            const i64 dc = std::max(0, std::max(target.c0 - c, c - target.c1));
            return (diagonal * std::min(dr, dc) + v.scale * (std::max(dr, dc) - std::min(dr, dc))) * v.weight_tenths / 10;
        };
        struct Entry {
            i64 estimate, cost;
            u32 cell;
            bool operator<(const Entry& o) const { return estimate != o.estimate ? estimate > o.estimate : cost < o.cost; }
        };
        std::priority_queue<Entry> heap;
        size_t top_level = 0, level = 0;
        auto push = [&](u32 cell, i64 cost) {
            i64 estimate = cost + heuristic(cell);
            if (!v.buckets) {
                heap.push({estimate, cost, cell});
                return;
            }
            if (size_t(estimate) >= levels.size()) levels.resize(size_t(estimate) * 2 + 1024);
            levels[size_t(estimate)].push_back(cell);
            top_level = std::max(top_level, size_t(estimate) + 1);
            level = std::min(level, size_t(estimate));  // a weighted estimate can fall below the level being emptied
        };
        auto offer = [&](u32 from, u32 to, i64 cost) {
            if (seen[to] == search_id && best[to] <= cost) return;
            seen[to] = search_id, best[to] = cost, parent[to] = from;
            push(to, cost);
        };
        for (u32 cell : net.tree) {
            seen[cell] = search_id, best[cell] = 0, parent[cell] = cell;
            push(cell, 0);
        }
        u32 reached = UINT32_MAX;
        for (;;) {
            u32 cell;
            if (v.buckets) {
                while (level < top_level && levels[level].empty()) ++level;
                if (level >= top_level) break;
                cell = levels[level].back();
                levels[level].pop_back();
                if (closed[cell] == search_id || size_t(best[cell] + heuristic(cell)) != level) continue;
            } else {
                if (heap.empty()) break;
                Entry top = heap.top();
                heap.pop();
                cell = top.cell;
                if (closed[cell] == search_id || top.cost != best[cell]) continue;
            }
            closed[cell] = search_id;
            ++expansions;
            if (is_target[cell] == search_id) {
                reached = cell;
                break;
            }
            const u32 in_plane = cell % plane;
            const int r = int(in_plane / cols), c = int(in_plane % cols);
            const i64 here = best[cell];
            const bool east = c + 1 < cols && enterable(cell + 1, net), west = c > 0 && enterable(cell - 1, net);
            const bool south = r + 1 < rows && enterable(cell + cols, net), north = r > 0 && enterable(cell - cols, net);
            if (east) offer(cell, cell + 1, here + units(multiple(cell + 1, net.cls), v.scale));
            if (west) offer(cell, cell - 1, here + units(multiple(cell - 1, net.cls), v.scale));
            if (south) offer(cell, cell + cols, here + units(multiple(cell + cols, net.cls), v.scale));
            if (north) offer(cell, cell - cols, here + units(multiple(cell - cols, net.cls), v.scale));
            auto slant = [&](bool a, bool c2, u32 next) {
                // A diagonal step is allowed only when both cells it brushes past are open.
                if (a && c2 && enterable(next, net)) offer(cell, next, here + units(multiple(next, net.cls), diagonal));
            };
            slant(south, east, cell + cols + 1);
            slant(south, west, cell + cols - 1);
            slant(north, east, cell - cols + 1);
            slant(north, west, cell - cols - 1);
            const int16_t drill = b.via_static[net.cls][in_plane];
            if (b.layers > 1 && (drill == FREE || drill == net.id)) {
                if (via_seen[in_plane] != search_id) via_seen[in_plane] = search_id, via_price[in_plane] = via_units(in_plane, net.cls);
                const i64 hop = here + via_price[in_plane];
                for (int l = 0; l < b.layers; ++l) {
                    u32 next = u32(l * plane + in_plane);
                    if (next != cell) offer(cell, next, hop);
                }
            }
        }
        for (size_t l = level; l < top_level; ++l) levels[l].clear();
        if (reached == UINT32_MAX) return false;
        std::vector<u32> walked;
        for (u32 cell = reached;; cell = parent[cell]) {
            walked.push_back(cell);
            net.tree.push_back(cell);
            add_to_path(net, cell);
            u32 from = parent[cell];
            if (from == cell) break;
            if (from % plane == cell % plane) {  // a layer change: a via and its blob
                const u32 in_plane = cell % plane;
                const int r = int(in_plane / cols), c = int(in_plane % cols);
                ++net.via_count;
                for (int l = 0; l < b.layers; ++l) {
                    net.tree.push_back(u32(l * plane + in_plane));
                    for (auto [dr, dc] : blob[net.cls]) add_to_path(net, u32(l * plane + size_t(r + dr) * cols + (c + dc)));
                }
            }
        }
        net.connections.push_back(std::move(walked));
        for (u32 cell : target.cells)
            if (enterable(cell, net)) net.tree.push_back(cell);
        return true;
    }

    void route(Net& net) {
        ++path_id;
        std::vector<char> joined(net.pads.size(), 0);
        if (!net.preset.empty() || !net.preset_vias.empty()) {
            const size_t plane = b.plane();
            ++search_id;
            for (u32 cell : net.preset) add_to_path(net, cell), net.tree.push_back(cell), seen[cell] = search_id;
            for (u32 in_plane : net.preset_vias) {
                const int r = int(in_plane / b.cols), c = int(in_plane % b.cols);
                ++net.via_count;
                for (int l = 0; l < b.layers; ++l) {
                    const u32 cell = u32(l * plane + in_plane);
                    net.tree.push_back(cell), seen[cell] = search_id;
                    for (auto [dr, dc] : blob[net.cls]) {
                        const int rr = r + dr, cc = c + dc;
                        if (rr >= 0 && rr < b.rows && cc >= 0 && cc < b.cols) add_to_path(net, u32(l * plane + size_t(rr) * b.cols + cc));
                    }
                }
            }
            for (size_t i = 0; i < net.pads.size(); ++i)
                for (u32 cell : net.pads[i].cells)
                    if (seen[cell] == search_id) { joined[i] = 1; break; }
            net.kept = true;
            net.preset_n = net.path.size();
            for (size_t i = 0; i < net.pads.size(); ++i)
                if (joined[i])
                    for (u32 cell : net.pads[i].cells)
                        if (enterable(cell, net)) net.tree.push_back(cell);
        }
        if (std::find(joined.begin(), joined.end(), 1) == joined.end()) {
            joined[0] = 1;
            for (u32 cell : net.pads[0].cells)
                if (enterable(cell, net)) net.tree.push_back(cell);
        }
        auto start = std::chrono::steady_clock::now();
        for (size_t done = size_t(std::count(joined.begin(), joined.end(), 1)); done < net.pads.size(); ++done) {
            int pick = -1, nearest = INT32_MAX;
            for (size_t i = 0; i < net.pads.size(); ++i) {
                if (joined[i]) continue;
                for (size_t j = 0; j < net.pads.size(); ++j) {
                    if (!joined[j]) continue;
                    const Pad &a = net.pads[i], &p = net.pads[j];
                    int gap = std::max(0, std::max(a.r0 - p.r1, p.r0 - a.r1)) + std::max(0, std::max(a.c0 - p.c1, p.c0 - a.c1));
                    if (gap < nearest) nearest = gap, pick = int(i);
                }
            }
            if (pick == net.blocked_pad || !connect(net, net.pads[pick])) {
                // Leave this pad out and go on with the rest: one unreachable pad must not cost
                // the net all its other connections.
                net.blocked_pad = pick;
                net.dead = true;
            }
            joined[pick] = 1;
        }
        search_ns += std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - start).count();
        // A net with an unreachable pad keeps the connections it did make.
        lay_footprint(net);
    }
};

// For each net, with no other net's tracks on the board: which pads can a track reach from the
// first pad, under the same moves and the same hard obstacles the router uses? Prints one line
// per net: id, class, pads, pads with no cell a track may stand on, pads reached, cells reached.
static void report_reach(const Board& b) {
    const size_t plane = b.plane(), n = b.cells();
    const int cols = b.cols, rows = b.rows;
    std::vector<u32> seen(n, 0), frontier;
    u32 stamp = 0;
    printf("net,class,pads,sealed_pads,reached_pads,reached_cells\n");
    for (const Net& net : b.nets) {
        auto open = [&](u32 cell) {
            const int16_t who = b.owner[net.cls][cell];
            return who == FREE || who == net.id;
        };
        int sealed = 0;
        for (const Pad& pad : net.pads) sealed += std::none_of(pad.cells.begin(), pad.cells.end(), open);
        ++stamp;
        frontier.clear();
        for (u32 cell : net.pads[0].cells)
            if (open(cell)) seen[cell] = stamp, frontier.push_back(cell);
        long cells = 0;
        while (!frontier.empty()) {
            const u32 cell = frontier.back();
            frontier.pop_back();
            ++cells;
            const u32 in_plane = cell % plane;
            const int r = int(in_plane / cols), c = int(in_plane % cols);
            auto visit = [&](u32 next) {
                if (seen[next] != stamp) seen[next] = stamp, frontier.push_back(next);
            };
            const bool east = c + 1 < cols && open(cell + 1), west = c > 0 && open(cell - 1);
            const bool south = r + 1 < rows && open(cell + cols), north = r > 0 && open(cell - cols);
            if (east) visit(cell + 1);
            if (west) visit(cell - 1);
            if (south) visit(cell + cols);
            if (north) visit(cell - cols);
            if (south && east && open(cell + cols + 1)) visit(cell + cols + 1);
            if (south && west && open(cell + cols - 1)) visit(cell + cols - 1);
            if (north && east && open(cell - cols + 1)) visit(cell - cols + 1);
            if (north && west && open(cell - cols - 1)) visit(cell - cols - 1);
            const int16_t drill = b.via_static[net.cls][in_plane];
            if (b.layers > 1 && (drill == FREE || drill == net.id))
                for (int l = 0; l < b.layers; ++l) visit(u32(l * plane + in_plane));
        }
        int reached = 0;
        for (const Pad& pad : net.pads)
            reached += std::any_of(pad.cells.begin(), pad.cells.end(), [&](u32 cell) { return seen[cell] == stamp; });
        printf("%d,%d,%zu,%d,%d,%ld\n", net.id, net.cls, net.pads.size(), sealed, reached, cells);
    }
}

// Usage: negotiate6 <board> <label> <order> <seed> <present_start> <present_growth> <history_step> <via_factor> [dump [weight_tenths]]
int main(int argc, char** argv) {
    if (argc < 4) return 1;
    Board board;
    if (!load(argv[1], board)) {
        fprintf(stderr, "cannot read %s\n", argv[1]);
        return 1;
    }
    if (std::string(argv[3]) == "reach") {
        report_reach(board);
        return 0;
    }
    if (argc < 9) return 1;
    Config config;
    config.order = argv[3], config.seed = unsigned(atoll(argv[4]));
    config.present_start = atof(argv[5]), config.present_growth = atof(argv[6]);
    config.history_step = float(atof(argv[7])), config.via_factor = atof(argv[8]);
    Variant variant{10, 4096, true, config.via_factor, argc > 10 ? atoll(argv[10]) : 10};
    Router router(board, variant);
    std::vector<Net*> order;
    for (Net& net : board.nets) order.push_back(&net);
    auto area = [](const Net* net) {
        int r0 = 1 << 30, r1 = 0, c0 = 1 << 30, c1 = 0;
        for (const Pad& pad : net->pads)
            r0 = std::min(r0, pad.r0), r1 = std::max(r1, pad.r1), c0 = std::min(c0, pad.c0), c1 = std::max(c1, pad.c1);
        return i64(r1 - r0 + 1) * (c1 - c0 + 1);
    };
    if (config.order == "span_asc")
        std::stable_sort(order.begin(), order.end(), [](const Net* a, const Net* c) { return a->span < c->span; });
    else if (config.order == "span_desc")
        std::stable_sort(order.begin(), order.end(), [](const Net* a, const Net* c) { return a->span > c->span; });
    else if (config.order == "pads_desc")
        std::stable_sort(order.begin(), order.end(), [](const Net* a, const Net* c) { return a->pads.size() > c->pads.size(); });
    else if (config.order == "area_desc")
        std::stable_sort(order.begin(), order.end(), [&](const Net* a, const Net* c) { return area(a) > area(c); });
    else if (config.order == "random") {
        std::mt19937 rng(config.seed);
        std::shuffle(order.begin(), order.end(), rng);
    } else {
        fprintf(stderr, "unknown order %s\n", config.order.c_str());
        return 1;
    }

    auto begin = std::chrono::steady_clock::now();
    // NEGOTIATE_ONLY: route only these net codes (comma separated), as if the others were absent.
    if (const char* only = getenv("NEGOTIATE_ONLY")) {
        std::vector<int> keep;
        for (const char* p = only; *p;) {
            keep.push_back(atoi(p));
            while (*p && *p != ',') ++p;
            if (*p == ',') ++p;
        }
        std::erase_if(order, [&](const Net* net) { return std::find(keep.begin(), keep.end(), net->id) == keep.end(); });
    }
    // A non-zero seed jitters every cell's starting price a little, so a retry finds different
    // routes even where the net order alone changes nothing.
    if (config.seed > 0) {
        std::mt19937 rng(config.seed);
        std::uniform_real_distribution<float> jitter(0.0f, 0.05f);
        for (float& h : router.history) h = jitter(rng);
    }
    // NEGOTIATE_HISTORY_IN: start from a saved congestion history (raw float32 per cell), scaled by
    // NEGOTIATE_HISTORY_SCALE (default 1). Used to measure what a predicted congestion map could save.
    if (const char* in = getenv("NEGOTIATE_HISTORY_IN")) {
        FILE* f = fopen(in, "rb");
        if (!f) { fprintf(stderr, "cannot read %s\n", in); return 1; }
        const size_t got = fread(router.history.data(), sizeof(float), router.history.size(), f);
        fclose(f);
        if (got != router.history.size()) { fprintf(stderr, "history size mismatch\n"); return 1; }
        const float scale = getenv("NEGOTIATE_HISTORY_SCALE") ? float(atof(getenv("NEGOTIATE_HISTORY_SCALE"))) : 1.0f;
        for (float& h : router.history) h *= scale;
    }
    std::signal(SIGTERM, request_stop);
    std::signal(SIGINT, request_stop);
    // NEGOTIATE_PRESET: the previous session's route still standing, per net (int32 net id, cell
    // count, via count, then uint32 cells and uint32 in-plane via positions).
    if (const char* in = getenv("NEGOTIATE_PRESET")) {
        FILE* f = fopen(in, "rb");
        if (!f) { fprintf(stderr, "cannot read %s\n", in); return 1; }
        int32_t count = 0;
        if (fread(&count, 4, 1, f) != 1) count = 0;
        for (int i = 0; i < count; ++i) {
            int32_t head[3];
            if (fread(head, 4, 3, f) != 3) break;
            std::vector<u32> cells(static_cast<size_t>(head[1])), vias(static_cast<size_t>(head[2]));
            if (fread(cells.data(), 4, cells.size(), f) != cells.size() || fread(vias.data(), 4, vias.size(), f) != vias.size()) break;
            for (Net& net : board.nets)
                if (net.id == head[0]) net.preset = std::move(cells), net.preset_vias = std::move(vias);
        }
        fclose(f);
    }
    // Nets with a preset go first: laying them costs nothing, and the new routing then sees them.
    std::stable_partition(order.begin(), order.end(), [](const Net* net) { return !net->preset.empty() || !net->preset_vias.empty(); });
    if (const char* price = getenv("NEGOTIATE_KEPT_PRICE")) router.kept_price = atof(price);
    // With kept copper on the board, new connections pay to cross it from the first round.
    for (const Net& net : board.nets)
        if (!net.preset.empty() || !net.preset_vias.empty()) { router.present = config.present_start; break; }
    const int max_rounds = getenv("NEGOTIATE_MAX_ROUNDS") ? std::max(1, atoi(getenv("NEGOTIATE_MAX_ROUNDS"))) : MAX_ITERATIONS;
    printf("Starting auto-routing...\n");
    fflush(stdout);
    for (Net* net : order) {
        if (stop_requested) break;
        router.route(*net);
    }
    int iterations = 1, conflicts = 0;
    const int patience = getenv("NEGOTIATE_PATIENCE") ? atoi(getenv("NEGOTIATE_PATIENCE")) : 0;
    // NEGOTIATE_WARMUP: never give up for lack of progress before this round. Under a gentle schedule the
    // conflict count often holds level for the first rounds while prices build; patience alone then stops
    // runs that would have converged (negotiate3 behaviour = 0).
    const int warmup = getenv("NEGOTIATE_WARMUP") ? atoi(getenv("NEGOTIATE_WARMUP")) : 0;
    int fewest = INT32_MAX, last_gain = 0;
    const double budget_s = getenv("NEGOTIATE_SECONDS") ? atof(getenv("NEGOTIATE_SECONDS")) : 0;  // stop starting new rounds after this long
    const bool trace = getenv("NEGOTIATE_TRACE") != nullptr;  // one line per iteration: conflicts, dead nets, expansions, seconds
    std::vector<Net*> redo;
    for (;; ++iterations) {
        redo.clear();
        int in_conflict_count = 0;
        for (Net* net : order) {
            bool on_kept = false;
            const bool in_conflict = router.conflicted(*net, &on_kept);
            in_conflict_count += in_conflict;
            // Rounds in a row in which new copper sits on this net's kept copper.
            net->conflict_rounds = on_kept ? net->conflict_rounds + 1 : 0;
            // Kept copper gets three rounds for the new routes to move off it before it is ripped up.
            if (in_conflict) redo.push_back(net);
        }
        conflicts = in_conflict_count;
        if (trace) {
            int dead_now = 0;
            for (const Net& net : board.nets) dead_now += net.dead;
            fprintf(stderr, "trace,%d,%d,%d,%ld,%.1f\n", iterations, conflicts, dead_now, router.expansions,
                    std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count());
        }
        if (conflicts < fewest) fewest = conflicts, last_gain = iterations;
        // With patience set, give up once the count of conflicted nets has stopped falling.
        if (stop_requested || conflicts == 0 || iterations >= max_rounds || (patience > 0 && iterations >= warmup && iterations - last_gain >= patience)) break;
        if (budget_s > 0 && std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count() > budget_s) break;
        for (Net* net : redo)
            for (u32 cell : net->path)
                if (router.occupancy[net->cls][cell] + router.preset_occ[net->cls][cell] > 1) router.history[cell] += config.history_step;
        router.present = router.present == 0 ? config.present_start : std::min(PRESENT_LIMIT, router.present * config.present_growth);
        for (Net* net : redo) {
            if (stop_requested) break;  // the nets not yet taken up keep the copper they have
            // A kept net reroutes only its new connections, unless other nets' new copper has sat on
            // its kept copper for more than three rounds: then the whole net is routed afresh.
            router.rip_up(*net, net->kept && net->conflict_rounds <= 3);
            net->dead = false;
            router.route(*net);
        }
    }
    double total_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - begin).count();
    // NEGOTIATE_HISTORY_OUT: save the congestion history as it stands at the end.
    if (const char* out = getenv("NEGOTIATE_HISTORY_OUT")) {
        FILE* f = fopen(out, "wb");
        if (f) { fwrite(router.history.data(), sizeof(float), router.history.size(), f); fclose(f); }
    }

    // NEGOTIATE_CONFLICTS: for each net left in conflict, how many of its cells are shared and how
    // many of those lie within 0.4 mm (8 cells) of any pad.
    if (getenv("NEGOTIATE_CONFLICTS")) {
        const size_t plane = board.plane();
        std::vector<char> near_pad(plane, 0);
        for (const Net& net : board.nets)
            for (const Pad& pad : net.pads)
                for (u32 cell : pad.cells) {
                    const int r = int((cell % plane) / board.cols), c = int(cell % board.cols);
                    for (int dr = -8; dr <= 8; ++dr)
                        for (int dc = -8; dc <= 8; ++dc) {
                            const int rr = r + dr, cc = c + dc;
                            if (rr >= 0 && rr < board.rows && cc >= 0 && cc < board.cols) near_pad[size_t(rr) * board.cols + cc] = 1;
                        }
                }
        for (Net& net : board.nets) {
            if (!router.conflicted(net)) continue;
            {
                long in_preset = 0, in_new = 0;
                for (size_t i = 0; i < net.path.size(); ++i) {
                    const u32 cell = net.path[i];
                    if (router.occupancy[net.cls][cell] + router.preset_occ[net.cls][cell] > 1) (i < net.preset_n ? in_preset : in_new) += 1;
                }
                fprintf(stderr, "conflict_split,%d,kept=%d,preset_n=%zu,path=%zu,in_preset=%ld,in_new=%ld,conns=%zu\n", net.id, int(net.kept), net.preset_n, net.path.size(), in_preset, in_new, net.connections.size());
            }
            long shared = 0, by_pad = 0;
            u32 first = UINT32_MAX;
            for (u32 cell : net.path)
                if (router.occupancy[net.cls][cell] > 1) {
                    ++shared, by_pad += near_pad[cell % plane];
                    if (first == UINT32_MAX) first = cell;
                }
            fprintf(stderr, "conflict,%d,%ld,%ld,%zu,%u\n", net.id, shared, by_pad, net.pads.size(), first);
        }
    }
    long vias = 0;
    int dead = 0;
    for (const Net& net : board.nets) vias += net.via_count, dead += net.dead;
    printf("%s,%s,%zu,%zu,%d,%d,%d,%d,%ld,%ld,%.3f,%.3f\n", argv[1], argv[2], board.cells(), board.nets.size(), dead,
           conflicts == 0, iterations, conflicts, vias, router.expansions, router.search_ns / 1e6, total_ms);

    if (stop_requested)  // stopped before every net was routed: say which have nothing
        for (Net& net : board.nets)
            if (net.connections.empty() && !net.kept && net.pads.size() > 1) net.dead = true;
    if (const char* kept = getenv("NEGOTIATE_KEPT_OUT")) {
        FILE* f = fopen(kept, "w");
        if (f) {
            for (const Net& net : board.nets)
                if (net.kept) fprintf(f, "%d\n", net.id);
            fclose(f);
        }
    }
    if (argc > 9) {
        FILE* out = fopen(argv[9], "wb");
        int32_t count = int32_t(board.nets.size());
        fwrite(&count, 4, 1, out);
        for (const Net& net : board.nets) {
            int32_t head[4] = {net.id, net.dead, router.conflicted(net), int32_t(net.connections.size())};
            fwrite(head, 4, 4, out);
            for (const auto& walked : net.connections) {
                int32_t size = int32_t(walked.size());
                fwrite(&size, 4, 1, out);
                fwrite(walked.data(), 4, walked.size(), out);
            }
        }
        fclose(out);
    }
    return 0;
}
