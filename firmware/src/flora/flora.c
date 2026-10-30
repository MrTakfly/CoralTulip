#include "flora.h"
#include "flora_lut.h"
#include "flora_pal.h"

/* 80s Synthwave / Outrun Visualizer Engine for CoralTulip.
 *
 * Replaces the botanical flora/coral renderer while preserving the exact flora_t struct and API:
 *   - FL_SP_PLANT (Top algorithms): Outrun Sunset scene — a giant segmented retro sun over a 3D triangulated
 *     wireframe mountain range and a scrolling 3D perspective neon highway grid.
 *   - FL_SP_CORAL (Side algorithms): Cyber Skyline scene — a rotating 3D wireframe synth crystal / octahedron
 *     over a neon cyberpunk skyscraper skyline and an electric blue perspective grid.
 *
 * Musical mapping across the 12 pitch classes (0..11):
 *   - Each pitch class owns one of the 12 perspective grid lanes and its corresponding horizon peak / cyber tower.
 *   - Active chord tones raise their wireframe mountain peak or skyscraper tower to a height and shape determined
 *     by harmonic role (Root, 3rd, 5th, 7th, 9th, tension) and chord quality (Major, Minor, Dim, Aug, Sus).
 *   - Note onsets (keys, sequencer, or arpeggiator) fire high-speed neon light pulses down that note's 3D grid lane,
 *     flash its horizon peak, and launch synth laser sparks into the sky.
 *   - Audio loudness (f->level) expands the Outrun sun, intensifies the horizon glow, and drives the 3D crystal spin.
 */

enum { R_ROOT = 0, R_THIRD, R_FIFTH, R_FLAT5, R_SHARP5, R_SEVENTH, R_NINTH, R_FLAT9, R_ELEVENTH, R_SIXTH };
enum { P_SPARK = 0, P_LASER, P_GLINT, P_RING_DOT };

#define HZ_Y 43 /* horizon row on the 120x70 canvas */

/* ---- integer helpers ---------------------------------------------------------------------------------------- */

static int32_t fl_fsin(int32_t a)
{
    a &= 1023;
    if (a < 256)
        return fl_qsin[a];
    if (a < 512)
        return fl_qsin[512 - a];
    if (a < 768)
        return -fl_qsin[a - 512];
    return -fl_qsin[1024 - a];
}
static int32_t fl_fcos(int32_t a) { return fl_fsin(a + 256); }

static uint32_t fl_hash32(uint32_t x)
{
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

static uint32_t fl_rnd(flora_t *f)
{
    uint32_t x = f->rng ? f->rng : 0x9E3779B9u;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return f->rng = x;
}

static int32_t fl_clampi(int32_t v, int32_t lo, int32_t hi) { return v < lo ? lo : v > hi ? hi : v; }

static int32_t fl_approach(int32_t v, int32_t t, uint32_t dt, int32_t tau)
{
    int32_t d = t - v, s;
    if (d == 0)
        return v;
    s = d * (int32_t)(dt < 200 ? dt : 200) / tau;
    if (s == 0)
        s = d > 0 ? 1 : -1;
    if ((d > 0 && s > d) || (d < 0 && s < d))
        s = d;
    return v + s;
}

static int fl_role_of(int interval, int quality)
{
    switch (interval) {
    case 0: return R_ROOT;
    case 1: return R_FLAT9;
    case 2: return quality == FL_Q_SUS2 ? R_ELEVENTH : R_NINTH;
    case 3:
    case 4: return R_THIRD;
    case 5: return R_ELEVENTH;
    case 6: return R_FLAT5;
    case 7: return R_FIFTH;
    case 8: return R_SHARP5;
    case 9: return R_SIXTH;
    default: return R_SEVENTH;
    }
}

static void fl_spawn(flora_t *f, int32_t x, int32_t y, int vx, int vy, int life, uint8_t col, uint8_t kind)
{
    int i, best = 0;
    for (i = 0; i < FL_MAX_PART; i++) {
        if (f->part[i].life == 0) {
            best = i;
            break;
        }
        if (f->part[i].life < f->part[best].life)
            best = i;
    }
    f->part[best].x = x;
    f->part[best].y = y;
    f->part[best].vx = (int16_t)vx;
    f->part[best].vy = (int16_t)vy;
    f->part[best].life = (uint16_t)life;
    f->part[best].col = col;
    f->part[best].kind = kind;
}

/* Horizon X coordinate (0..119) for each of the 12 pitch classes (0..11).
 * Arranged so the 12 pitch classes span across the horizon, flanking the central Outrun sun cleanly:
 * slots 0..5 on the left (x = 7..49), slots 6..11 on the right (x = 71..113). */
static int fl_pc_x(int pc)
{
    pc = ((pc % 12) + 12) % 12;
    if (pc < 6)
        return 7 + pc * 8;
    return 73 + (pc - 6) * 8;
}

/* ---- building the synthwave skyline / mountain targets from a chord ---------------------------------------- */

static void fl_build(flora_t *f)
{
    /* Synthwave neon ramps around the circle of fifths */
    static const uint8_t SYNTH_RAMPS[6] = {FL_R_ORCHID, FL_R_SEA, FL_R_ROSE, FL_R_FIRE, FL_R_JADE, FL_R_PEACH};
    static const uint8_t CYBER_RAMPS[6] = {FL_R_SEA, FL_R_ORCHID, FL_R_JADE, FL_R_ROSE, FL_R_FIRE, FL_R_LIME};
    const fl_chord_t *c = &f->shape;
    int i, n = c->n > 8 ? 8 : c->n, q = c->quality;
    int fifth = (c->root % 12) * 7 % 12, home = (f->home % 12) * 7 % 12;
    uint16_t active_mask = 0;

    f->key_ramp = f->bloom ? (uint8_t)(f->bloom - 1u) : SYNTH_RAMPS[fifth % 6];
    f->eye_ramp = f->key_ramp == FL_R_FIRE ? FL_R_ORCHID : FL_R_FIRE;
    f->body_ramp = CYBER_RAMPS[home % 6];

    for (i = 0; i < n; i++)
        active_mask |= (uint16_t)(1u << (c->note[i] % 12));

    f->nseg = 12;
    for (i = 0; i < 12; i++) {
        fl_seg_t *s = &f->seg[i];
        int was_alive = s->alive;
        int on = (active_mask >> i) & 1u;
        int iv = (i - (c->root % 12) + 12) % 12;
        int role = fl_role_of(iv, q);
        int32_t base_h = 4 + (int32_t)(fl_hash32((uint32_t)i * 97u + 13u) % 4u);
        int32_t h_px = base_h;
        int w_px = 7;

        s->key = (uint16_t)(i + 1);
        s->parent = -1;
        s->slot = (uint8_t)i;
        s->depth = 0;
        s->sx = (int16_t)fl_pc_x(i);

        if (on) {
            switch (role) {
            case R_ROOT:
                h_px = 23 + (n >= 4 ? 3 : 0);
                w_px = 12;
                s->ramp = f->key_ramp;
                break;
            case R_THIRD:
                h_px = q == FL_Q_MINOR ? 20 : 18;
                w_px = q == FL_Q_MAJOR ? 11 : 9;
                s->ramp = f->key_ramp;
                break;
            case R_FIFTH:
                h_px = 16;
                w_px = 9;
                s->ramp = f->body_ramp;
                break;
            case R_SEVENTH:
                h_px = 19;
                w_px = 8;
                s->ramp = f->eye_ramp;
                break;
            case R_NINTH:
            case R_SIXTH:
                h_px = 15;
                w_px = 8;
                s->ramp = FL_R_SEA;
                break;
            default: /* tensions: sharp jagged neon spires */
                h_px = 17;
                w_px = 6;
                s->ramp = FL_R_FIRE;
                break;
            }
            if (q == FL_Q_DIM)
                w_px = w_px * 3 / 4;
            else if (q == FL_Q_AUG)
                w_px = w_px * 5 / 4;
        } else {
            s->ramp = f->species == FL_SP_CORAL ? FL_R_SEA : FL_R_ORCHID;
        }

        s->kind = (uint8_t)role;
        s->alive = (uint8_t)on;
        s->len_t = (int16_t)(h_px << 8);
        s->w_t = (uint8_t)w_px;
        if (s->len == 0) {
            s->len = (int16_t)(base_h << 8);
            s->w = (uint8_t)w_px;
        }
        s->sy = (int16_t)(HZ_Y - (s->len >> 8));

        /* Release a burst of retro synth sparks when a peak drops */
        if (was_alive && !on && s->sx > 0) {
            fl_spawn(f, (int32_t)s->sx << 8, (int32_t)s->sy << 8,
                     (int)(fl_rnd(f) % 48) - 24, -28 - (int)(fl_rnd(f) % 24),
                     900 + (int)(fl_rnd(f) % 600), FL_C(f->key_ramp, 0), P_SPARK);
        }
    }
}

/* ---- public API --------------------------------------------------------------------------------------------- */

void flora_init(flora_t *f, uint32_t seed, int cx, int cy)
{
    uint8_t *p = (uint8_t *)f;
    unsigned i;
    fl_chord_t c;
    for (i = 0; i < sizeof(*f); i++)
        p[i] = 0;
    f->rng = seed ? seed : 0x9E3779B9u;
    f->cx = (int16_t)cx;
    f->cy = (int16_t)cy;
    f->yaw = 160 << 8;
    c.n = 3;
    c.note[0] = 60;
    c.note[1] = 64;
    c.note[2] = 67;
    c.root = 0;
    c.quality = FL_Q_MAJOR;
    c.bass = 48;
    flora_set_chord(f, &c);
}

void flora_set_species(flora_t *f, int species)
{
    species = species ? FL_SP_CORAL : FL_SP_PLANT;
    if (species == f->species)
        return;
    f->species = (uint8_t)species;
    if (f->shape.n)
        fl_build(f);
}

void flora_set_key(flora_t *f, int tonic_pc)
{
    tonic_pc = ((tonic_pc % 12) + 12) % 12;
    if (tonic_pc == f->home)
        return;
    f->home = (uint8_t)tonic_pc;
    if (f->shape.n)
        fl_build(f);
}

void flora_set_chord(flora_t *f, const fl_chord_t *c)
{
    int i, same;
    if (c->n == 0) {
        f->chord.n = 0;
        return;
    }
    same = c->n == f->shape.n && c->root == f->shape.root && c->quality == f->shape.quality;
    for (i = 0; same && i < c->n; i++)
        same = c->note[i] == f->shape.note[i];
    f->chord = *c;
    if (same && f->nseg)
        return;
    f->shape = *c;
    fl_build(f);
}

void flora_note_on(flora_t *f, uint8_t note, uint8_t vel)
{
    int pc = note % 12;
    int sx = fl_pc_x(pc);
    int sy = HZ_Y - (f->seg[pc].len >> 8);
    uint32_t r = fl_rnd(f);
    f->pulse_t[pc] = f->t ? f->t : 1u;
    f->pulse_on[pc] = 1;
    f->pulse_n[pc]++;
    if (f->t - f->ring_t[f->ring_i & 3] > 180u || !f->ring_t[f->ring_i & 3]) {
        f->ring_i = (uint8_t)((f->ring_i + 1) & 3);
        f->ring_t[f->ring_i] = f->t ? f->t : 1u;
    }
    if (f->level_in < (int32_t)vel << 9)
        f->level_in = (int32_t)vel << 9;

    /* Spawn neon synth laser / spark from the note's peak */
    fl_spawn(f, (int32_t)sx << 8, (int32_t)sy << 8,
             (int)((r >> 4) % 41) - 20, -48 - (int)((r >> 10) % 40),
             900 + (int)((r >> 16) % 700),
             (r & 1) ? FL_C(f->key_ramp, 0) : FL_C(FL_R_SEA, 0),
             (r & 3) == 0 ? P_LASER : P_SPARK);
}

void flora_set_level(flora_t *f, uint16_t level) { f->level_in = level; }

void flora_tick(flora_t *f, uint32_t dt)
{
    int i;
    if (dt > 250)
        dt = 250;
    f->t += dt;
    f->yaw += (int32_t)dt * (6 + (f->level >> 13));

    if (f->level_in > f->level)
        f->level = fl_approach(f->level, f->level_in, dt, 35);
    else
        f->level = fl_approach(f->level, f->level_in, dt, 750);
    f->level_in = f->level_in * 15 / 16;

    for (i = 0; i < 12; i++) {
        fl_seg_t *s = &f->seg[i];
        int32_t target_len = s->len_t;
        if (s->alive && f->chord.n > 0)
            target_len += (f->level >> 13) << 8;
        if (f->pulse_on[i] && f->t - f->pulse_t[i] < 260u)
            target_len += (int32_t)(260u - (f->t - f->pulse_t[i])) * 3;
        s->len = (int16_t)fl_approach(s->len, target_len, dt, 140);
        s->w = (uint8_t)fl_approach(s->w, s->w_t, dt, 180);
        s->grow = (uint8_t)fl_approach(s->grow, s->alive ? 255 : 90, dt, 180);
        s->sx = (int16_t)fl_pc_x(i);
        s->sy = (int16_t)(HZ_Y - (s->len >> 8));
        if (f->pulse_on[i] && f->t - f->pulse_t[i] > 900u)
            f->pulse_on[i] = 0;
    }

    /* Ambient synthwave horizon glints */
    f->part_acc += dt * (1 + (f->level >> 13));
    while (f->part_acc > 380) {
        uint32_t r = fl_rnd(f);
        int pc = (int)(r % 12u);
        f->part_acc -= 380;
        if (f->seg[pc].alive) {
            fl_spawn(f, (int32_t)fl_pc_x(pc) << 8, (int32_t)(HZ_Y - (f->seg[pc].len >> 8)) << 8,
                     (int)((r >> 5) % 25) - 12, -24 - (int)((r >> 11) % 24),
                     1100 + (int)((r >> 17) % 800),
                     FL_C(f->key_ramp, 0), P_GLINT);
        }
    }

    for (i = 0; i < FL_MAX_PART; i++) {
        fl_part_t *p = &f->part[i];
        if (!p->life)
            continue;
        p->life = p->life > dt ? (uint16_t)(p->life - dt) : 0;
        p->x += p->vx * (int32_t)dt / 64;
        p->y += p->vy * (int32_t)dt / 64;
        if (p->y < -(2 << 8) || p->x < 0 || p->x >= (120 << 8))
            p->life = 0;
    }
}

/* ---- drawing the 80s synthwave scene ------------------------------------------------------------------------ */

static const uint8_t fl_bayer[16] = {0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5};
static const uint8_t FL_SKY_OUTRUN[5] = {FL_BG, FL_L0, FL_L1, FL_L2, FL_L3};
static const uint8_t FL_SKY_CYBER[5]  = {FL_BG, FL_S0, FL_S1, FL_S2, FL_S3};

static uint8_t fl_grad(const uint8_t *st, int n, int32_t t, int x, int y)
{
    int32_t i;
    if (t <= 0)
        return st[0];
    if (t >= (int32_t)(n - 1) << 8)
        return st[n - 1];
    i = t >> 8;
    return (int32_t)((t & 255) >> 4) > fl_bayer[(y & 3) * 4 + (x & 3)] ? st[i + 1] : st[i];
}

static void fl_put(flc_t *c, int x, int y, uint8_t col)
{
    if ((unsigned)x < (unsigned)c->w && (unsigned)y < (unsigned)c->h)
        c->px[y * c->w + x] = col;
}

static void fl_hline(flc_t *c, int x0, int x1, int y, uint8_t col)
{
    int x;
    if ((unsigned)y >= (unsigned)c->h)
        return;
    if (x0 < 0)
        x0 = 0;
    if (x1 >= c->w)
        x1 = c->w - 1;
    for (x = x0; x <= x1; x++)
        c->px[y * c->w + x] = col;
}

/* 1. Sky gradient + twinkling 80s starfield */
static void fl_draw_sky(flora_t *f, flc_t *c, int cyber)
{
    const uint8_t *st = cyber ? FL_SKY_CYBER : FL_SKY_OUTRUN;
    int W = c->w, x, y, i;
    int32_t boost = (f->level >> 9); /* louder = brighter horizon glow */

    if (f->backdrop == FL_BD_NONE) {
        flc_clear(c, FL_BG);
        return;
    }

    for (y = 0; y <= HZ_Y; y++) {
        int32_t t = y * y * 1024 / (HZ_Y * HZ_Y) + (y > HZ_Y / 2 ? boost * (y - HZ_Y / 2) / 16 : 0);
        for (x = 0; x < W; x++)
            c->px[y * W + x] = fl_grad(st, 5, t, x, y);
    }

    /* Twinkling stars & retro cross-stars in SCENE mode */
    if (f->backdrop == FL_BD_SCENE) {
        for (i = 0; i < 24; i++) {
            uint32_t h = fl_hash32((uint32_t)i * 7919u + 19u);
            int sx = (int)(h % (uint32_t)W);
            int sy = (int)((h >> 8) % 30u);
            int tw = (int)(((f->t >> 6) + h) % 40u);
            /* Keep clear of top-left chord label, top-right key label, and central sun */
            if ((sx < 40 && sy < 18) || (sx > 80 && sy < 14) || (sx > 40 && sx < 80 && sy > 8))
                continue;
            if (tw == 0 && (i & 3) == 0) {
                /* 4-point retro cross-star glint */
                fl_put(c, sx, sy, FL_INK);
                fl_put(c, sx - 1, sy, FL_C(FL_R_SEA, 1));
                fl_put(c, sx + 1, sy, FL_C(FL_R_SEA, 1));
                fl_put(c, sx, sy - 1, FL_C(FL_R_SEA, 1));
                fl_put(c, sx, sy + 1, FL_C(FL_R_SEA, 1));
            } else {
                fl_put(c, sx, sy, tw < 3 ? FL_INK : ((h >> 16) & 1) ? FL_C(FL_R_SAND, 1) : FL_C(FL_R_SEA, 1));
            }
        }
    }
}

/* 2. Expanding synthwave shockwave rings (RIPPLES mode) */
static void fl_draw_ripples(flora_t *f, flc_t *c)
{
    int i, cx = 60, cy = 26;
    uint32_t idle = f->t % 3600u;
    for (i = 0; i < 5; i++) {
        uint32_t age = i < 4 ? f->t - f->ring_t[i] : idle;
        int32_t r, a, steps, fade;
        uint8_t col;
        if ((i < 4 && !f->ring_t[i]) || age > 2000u || (i == 4 && f->level > 8000))
            continue;
        r = (int32_t)age * 42 / 1000 + 16;
        fade = 768 - (int32_t)age * 768 / 2000;
        if (i == 4)
            fade /= 2;
        col = fade > 480 ? FL_C(f->key_ramp, 0) : fade > 240 ? FL_C(f->key_ramp, 1) : FL_C(f->key_ramp, 2);
        steps = r * 6 + 16;
        for (a = 0; a < steps; a++) {
            int32_t ang = a * 1024 / steps;
            int x = cx + ((fl_fcos(ang) * r) >> 14);
            int y = cy + ((fl_fsin(ang) * r * 3 / 4) >> 14);
            if ((unsigned)x < (unsigned)c->w && y >= 0 && y < HZ_Y &&
                fl_bayer[(y & 3) * 4 + (x & 3)] * 28 < fade)
                c->px[y * c->w + x] = col;
        }
    }
}

/* 3A. The Iconic 80s Outrun Segmented Sunset Sun (Top mode) */
static void fl_draw_outrun_sun(flora_t *f, flc_t *c)
{
    int cx = 60, cy = 25;
    int r = 16 + (int)(f->level >> 15); /* 16..18 px radius */
    int r2 = r * r, halo2 = (r + 4) * (r + 4);
    int x, y;
    int scroll = (int)((f->t >> 7) & 3u);

    for (y = cy - r - 4; y <= cy + r + 2 && y < HZ_Y; y++) {
        if (y < 0)
            continue;
        for (x = cx - r - 4; x <= cx + r + 4; x++) {
            int dx = x - cx, dy = y - cy;
            int d2 = dx * dx + dy * dy;
            if (d2 <= r2) {
                /* Classic 80s horizontal scanline cutouts in the lower half of the sun */
                int rel_y = dy + r; /* 0 at top .. 2*r at bottom */
                if (dy >= -1) {
                    int band = (dy + 1 + scroll) % 4;
                    int cut_h = dy > 8 ? 2 : 1;
                    if (band < cut_h)
                        continue; /* leave the dusk sky showing through the slice! */
                }
                /* Vertical sun gradient: solar yellow -> warm orange -> neon coral -> hot magenta */
                uint8_t col;
                if (rel_y < r / 2)
                    col = FL_C(FL_R_FIRE, 0);
                else if (rel_y < r)
                    col = (rel_y & 1) && ((x ^ y) & 1) ? FL_C(FL_R_FIRE, 0) : FL_C(FL_R_FIRE, 1);
                else if (rel_y < r * 3 / 2)
                    col = FL_C(f->key_ramp, 1);
                else
                    col = FL_C(FL_R_ORCHID, 1);
                fl_put(c, x, y, col);
            } else if (d2 <= halo2 && f->backdrop != FL_BD_NONE) {
                /* Dithered neon solar corona */
                if (fl_bayer[(y & 3) * 4 + (x & 3)] < (halo2 - d2) * 8 / (halo2 - r2 + 1))
                    fl_put(c, x, y, FL_C(f->key_ramp, 2));
            }
        }
    }
}

/* 3B. Rotating 3D Wireframe Synth-Octahedron & Holo-Ring (Side mode) */
static void fl_draw_cyber_core(flora_t *f, flc_t *c)
{
    int32_t cx = 60 << 8, cy = 22 << 8;
    int32_t rad = (12 << 8) + (f->level >> 5);
    int32_t h = (14 << 8) + (f->level >> 5);
    int32_t yaw = (f->yaw >> 8) & 1023;
    int32_t vx[4], vy[4];
    int32_t tx = cx, ty = cy - h;
    int32_t bx = cx, by = cy + h * 3 / 4;
    int i, a;
    uint8_t edge_col = FL_C(f->key_ramp, 0);
    uint8_t sub_col = FL_C(f->body_ramp, 1);

    /* Outer holo-ring behind the crystal */
    for (a = 0; a < 64; a++) {
        int32_t ang = a * 16;
        int rx = 60 + ((fl_fcos(ang) * 18) >> 14);
        int ry = 22 + ((fl_fsin(ang) * 6) >> 14);
        if (a & 1)
            fl_put(c, rx, ry, FL_C(FL_R_SEA, 2));
    }

    for (i = 0; i < 4; i++) {
        int32_t ang = (yaw + i * 256) & 1023;
        vx[i] = cx + ((fl_fcos(ang) * rad) >> 14);
        vy[i] = cy + ((fl_fsin(ang) * (rad / 3)) >> 14);
    }

    /* Dark interior fill of the octahedron so stars don't bleed through */
    for (i = 0; i < 4; i++) {
        int ni = (i + 1) & 3;
        flc_tri(c, tx, ty, vx[i], vy[i], vx[ni], vy[ni], FL_S0);
        flc_tri(c, bx, by, vx[i], vy[i], vx[ni], vy[ni], FL_BG);
    }

    /* Bright wireframe edges */
    for (i = 0; i < 4; i++) {
        int ni = (i + 1) & 3;
        flc_line(c, vx[i], vy[i], vx[ni], vy[ni], edge_col);
        flc_line(c, tx, ty, vx[i], vy[i], edge_col);
        flc_line(c, bx, by, vx[i], vy[i], sub_col);
    }
    flc_dot(c, tx, ty, 1, FL_INK);
}

/* 4A. 3D Wireframe Mountain Range (Top mode: each of the 12 pitch classes forms a triangulated vector peak) */
static void fl_draw_mountains(flora_t *f, flc_t *c)
{
    int i;
    /* Pass 1: Background foothills first, then active chord peaks in front */
    int pass;
    for (pass = 0; pass < 2; pass++) {
        for (i = 0; i < 12; i++) {
            const fl_seg_t *s = &f->seg[i];
            int active = s->alive && f->chord.n > 0;
            int px = s->sx;
            int h = s->len >> 8;
            int py = HZ_Y - h;
            int hw = s->w;
            int lx = px - hw, rx = px + hw, mx = px + (i < 6 ? 2 : -2);
            int pulse = f->pulse_on[i] && (f->t - f->pulse_t[i] < 280u);
            uint8_t fill_l, fill_r, wire_col, ridge_col;

            if ((pass == 0 && active) || (pass == 1 && !active))
                continue;

            fill_l = active ? FL_BG2 : FL_L0;
            fill_r = active ? FL_BG1 : FL_BG;
            wire_col = pulse ? FL_INK : active ? FL_C(s->ramp, 0) : FL_BG3;
            ridge_col = pulse ? FL_C(s->ramp, 0) : active ? FL_C(s->ramp, 1) : FL_L1;

            /* 3D two-faceted mountain peak (left face + right face) */
            flc_tri(c, lx << 8, HZ_Y << 8, px << 8, py << 8, mx << 8, HZ_Y << 8, fill_l);
            flc_tri(c, mx << 8, HZ_Y << 8, px << 8, py << 8, rx << 8, HZ_Y << 8, fill_r);

            /* Internal wireframe contour lines on active peaks */
            if (active && h >= 10) {
                int hy = py + h / 2;
                int hlx = (lx + px) / 2, hmx = (mx + px) / 2, hrx = (rx + px) / 2;
                flc_line(c, hlx << 8, hy << 8, hmx << 8, (hy + 1) << 8, FL_C(s->ramp, 2));
                flc_line(c, hmx << 8, (hy + 1) << 8, hrx << 8, hy << 8, FL_C(s->ramp, 3));
            }

            /* Glowing neon wireframe ridges */
            flc_line(c, lx << 8, HZ_Y << 8, px << 8, py << 8, wire_col);
            flc_line(c, px << 8, py << 8, rx << 8, HZ_Y << 8, ridge_col);
            flc_line(c, px << 8, py << 8, mx << 8, HZ_Y << 8, ridge_col);

            /* Peak apex beacon + vertical laser flash on note strike */
            if (active) {
                fl_put(c, px, py, FL_INK);
                if (pulse) {
                    flc_line(c, px << 8, (py - 8) << 8, px << 8, py << 8, FL_C(s->ramp, 0));
                    fl_put(c, px, py - 9, FL_INK);
                }
            }
        }
    }
}

/* 4B. Cyber City Skyline (Side mode: 12 neon-outlined skyscrapers with lit window grids & antennas) */
static void fl_draw_skyline(flora_t *f, flc_t *c)
{
    int i, pass;
    for (pass = 0; pass < 2; pass++) {
        for (i = 0; i < 12; i++) {
            const fl_seg_t *s = &f->seg[i];
            int active = s->alive && f->chord.n > 0;
            int px = s->sx;
            int h = s->len >> 8;
            int top = HZ_Y - h;
            int hw = active ? 4 : 3;
            int x0 = px - hw, x1 = px + hw;
            int pulse = f->pulse_on[i] && (f->t - f->pulse_t[i] < 280u);
            uint8_t edge = pulse ? FL_INK : active ? FL_C(s->ramp, 0) : FL_S2;
            uint8_t win_col = pulse ? FL_INK : FL_C(s->ramp, 1);
            int x, y;

            if ((pass == 0 && active) || (pass == 1 && !active))
                continue;

            /* Tower body */
            for (y = top; y < HZ_Y; y++)
                fl_hline(c, x0, x1, y, active ? FL_BG1 : FL_BG);

            /* Neon roof & vertical edges */
            fl_hline(c, x0, x1, top, edge);
            for (y = top; y < HZ_Y; y++) {
                fl_put(c, x0, y, edge);
                fl_put(c, x1, y, active ? FL_C(s->ramp, 1) : FL_S1);
            }

            /* Lit skyscraper windows & spire antenna */
            if (active) {
                for (y = top + 2; y < HZ_Y - 1; y += 2)
                    for (x = x0 + 2; x <= x1 - 2; x += 2)
                        fl_put(c, x, y, win_col);
                flc_line(c, px << 8, (top - 4) << 8, px << 8, top << 8, edge);
                fl_put(c, px, top - 4, pulse || ((f->t >> 8) & 1) ? FL_INK : FL_C(FL_R_FIRE, 0));
            }
        }
    }
}

/* 5. Scrolling 3D Perspective Neon Grid (y = HZ_Y .. 69) */
static void fl_draw_grid(flora_t *f, flc_t *c, int cyber)
{
    int W = c->w, H = c->h, G_H = H - HZ_Y; /* 27 rows: 43..69 */
    int y, i;
    uint8_t base_grid = cyber ? FL_S2 : FL_BG3;
    uint8_t glow_grid = FL_C(f->key_ramp, 2);
    uint8_t hz_col = f->chord.n > 0 ? FL_C(f->key_ramp, 0) : FL_C(f->key_ramp, 1);
    int scroll = (int)((f->t * 3 / 16) & 63u);

    /* Dark synthwave ground plane with subtle horizon reflection */
    for (y = HZ_Y; y < H; y++) {
        int dy = y - HZ_Y;
        uint8_t bg = dy < 3 ? (cyber ? FL_S1 : FL_L1) : dy < 8 ? (cyber ? FL_S0 : FL_L0) : FL_BG;
        fl_hline(c, 0, W - 1, y, bg);
    }

    /* Scrolling horizontal perspective Z-bars (quadratic spacing from horizon to bottom) */
    for (i = 0; i < 7; i++) {
        int z = (i * 64 + scroll); /* 0 .. 448 */
        int dy = (z * z) * G_H / (448 * 448);
        int gy = HZ_Y + dy;
        if (gy > HZ_Y && gy < H) {
            uint8_t col = dy < 6 ? glow_grid : base_grid;
            fl_hline(c, 0, W - 1, gy, col);
        }
    }

    /* 13 radial perspective lines from vanishing point (60, HZ_Y - 6) forming 12 musical lanes */
    for (i = 0; i <= 12; i++) {
        int x_hz = 6 + i * 9;               /* 6 .. 114 at y = HZ_Y */
        int x_bot = -36 + i * 16;           /* -36 .. 156 at y = H - 1 (wide 3D fan!) */
        int lit_lane = (i < 12 && f->seg[i].alive && f->chord.n > 0) ||
                       (i > 0 && f->seg[i - 1].alive && f->chord.n > 0);
        uint8_t col = lit_lane ? FL_C(f->key_ramp, 1) : base_grid;
        flc_line(c, x_hz << 8, HZ_Y << 8, x_bot << 8, (H - 1) << 8, col);
    }

    /* Active chord lane highlights & racing note-on light-cycle pulses */
    for (i = 0; i < 12; i++) {
        int x0_hz = 6 + i * 9, x1_hz = x0_hz + 9;
        int x0_bot = -36 + i * 16, x1_bot = x0_bot + 16;

        /* When a note is struck, a bright neon energy bar races down its perspective lane */
        if (f->pulse_on[i]) {
            uint32_t age = f->t - f->pulse_t[i];
            if (age < 550u) {
                int32_t p = (int32_t)age * 256 / 550;
                int dy = (p * p / 256) * (G_H - 1) / 256;
                int py = HZ_Y + 1 + dy;
                if (py < H) {
                    int lx = x0_hz + (x0_bot - x0_hz) * dy / (G_H - 1);
                    int rx = x1_hz + (x1_bot - x1_hz) * dy / (G_H - 1);
                    fl_hline(c, lx + 1, rx - 1, py, FL_INK);
                    if (py > HZ_Y + 1)
                        fl_hline(c, lx + 1, rx - 1, py - 1, FL_C(f->seg[i].ramp, 0));
                    if (py + 1 < H)
                        fl_hline(c, lx + 1, rx - 1, py + 1, FL_C(f->seg[i].ramp, 0));
                }
            }
        }
    }

    /* Bright glowing horizon beam */
    fl_hline(c, 0, W - 1, HZ_Y, hz_col);
    if (f->level > 12000)
        fl_hline(c, 24, W - 25, HZ_Y, FL_INK);
}

static void fl_draw_part(flora_t *f, flc_t *c, int i)
{
    fl_part_t *p = &f->part[i];
    int x = (int)(p->x >> 8), y = (int)(p->y >> 8);
    uint8_t col = p->life < 250 ? FL_BG3 : p->col;
    switch (p->kind) {
    case P_LASER:
        fl_put(c, x, y, FL_INK);
        fl_put(c, x, y + 1, col);
        fl_put(c, x, y + 2, col);
        break;
    case P_GLINT:
        fl_put(c, x, y, (((f->t >> 6) + (uint32_t)i) & 3) == 0 ? FL_INK : col);
        break;
    default:
        fl_put(c, x, y, col);
        break;
    }
}

void flora_render(flora_t *f, flc_t *c)
{
    int i, cyber = (f->species == FL_SP_CORAL);

    /* 1. Synthwave Sky & Stars */
    fl_draw_sky(f, c, cyber);

    /* 2. Shockwave Rings in RIPPLES mode */
    if (f->backdrop == FL_BD_RIPPLES)
        fl_draw_ripples(f, c);

    /* 3. Outrun Sun (Top) or 3D Wireframe Synth-Core (Side) */
    if (cyber)
        fl_draw_cyber_core(f, c);
    else
        fl_draw_outrun_sun(f, c);

    /* 4. 3D Wireframe Mountain Range (Top) or Cyber Skyline (Side) */
    if (cyber)
        fl_draw_skyline(f, c);
    else
        fl_draw_mountains(f, c);

    /* 5. Scrolling 3D Perspective Neon Highway Grid */
    fl_draw_grid(f, c, cyber);

    /* 6. Synth Sparks & Laser Particles */
    for (i = 0; i < FL_MAX_PART; i++)
        if (f->part[i].life)
            fl_draw_part(f, c, i);
}
