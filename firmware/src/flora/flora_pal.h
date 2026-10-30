/* CoralTulip Synthwave palette: ten four-step neon synthwave ramps on deep midnight violet.
 *
 * Every pixel the flora visualizer and the UI draw is an index into this table; the platform turns the
 * indices into RGB565 once per frame (fl_pal_rgb). A ramp runs from light (step 0) to dark (step 3).
 */
#ifndef FLORA_PAL_H
#define FLORA_PAL_H

#include <stdint.h>

enum {
    FL_BG = 0,      /* the screen background, also "transparent" for sprites */
    FL_BG1 = 1,     /* panels */
    FL_BG2 = 2,     /* raised panels, inactive outlines */
    FL_BG3 = 3,     /* dividers */
    FL_INK = 4,     /* primary text */
    FL_INK2 = 5,    /* secondary text */
    FL_INK3 = 6,    /* disabled text */
    FL_BLACK = 7,   /* pure shadow */
    FL_RAMP0 = 8    /* first ramp entry */
};

enum {
    FL_R_FIRE = 0,   /* solar yellow -> outrun orange -> hot red -> deep crimson */
    FL_R_PEACH,      /* sunset gold -> neon coral -> magenta-red -> dark wine */
    FL_R_COPPER,     /* chrome gold -> amber -> burnt neon -> deep violet */
    FL_R_LIME,       /* cyber lime -> arcade green -> emerald -> deep teal */
    FL_R_JADE,       /* electric mint -> neon turquoise -> outrun teal -> abyss */
    FL_R_SEA,        /* laser cyan -> electric blue -> arcade cobalt -> midnight indigo */
    FL_R_ORCHID,     /* neon pink -> hot magenta -> electric purple -> deep violet */
    FL_R_ROSE,       /* laser rose -> hot pink -> neon crimson -> dark plum */
    FL_R_SAND,       /* chrome white -> silver lavender -> steel violet -> gunmetal */
    FL_R_SAGE,       /* holo ice -> cyber sky -> slate blue -> dark navy */
    FL_NRAMPS
};

#define FL_PAL_SIZE (FL_RAMP0 + FL_NRAMPS * 4)

/* palette index of ramp r, step s (0 light .. 3 dark) */
#define FL_C(r, s) ((uint8_t)(FL_RAMP0 + (r) * 4 + (s)))

/* 0xRRGGBB */
static const uint32_t fl_pal_rgb[FL_PAL_SIZE] = {
    0x070312, 0x150B2A, 0x251444, 0x3D226A, 0xF6F2FF, 0xBCA8E8, 0x6E58A0, 0x000000,
    0xFFF05A, 0xFF7B1A, 0xD91E48, 0x6E0A2E,   /* FIRE */
    0xFFCA7A, 0xFF5E6C, 0xB81D58, 0x520C32,   /* PEACH */
    0xFFE07D, 0xF08C28, 0x9E383C, 0x4A1828,   /* COPPER */
    0xD8FF4E, 0x36E86B, 0x128858, 0x0A3D38,   /* LIME */
    0x72FFE0, 0x14D4B4, 0x0E7488, 0x09364A,   /* JADE */
    0x5CEEFF, 0x12A8FF, 0x1E4ED8, 0x161B6E,   /* SEA */
    0xFF88F8, 0xF828B8, 0x9B18A8, 0x460C62,   /* ORCHID */
    0xFF94C8, 0xFF2A78, 0xB81258, 0x5A0836,   /* ROSE */
    0xF6F4FF, 0xB2B0D0, 0x6A6690, 0x363252,   /* SAND */
    0xE0F8FF, 0x7AB4D8, 0x3E648E, 0x1E2E4E,   /* SAGE */
};

/* the backdrop's dark tints, dark to light: outrun sunset behind Top mode, cyber grid behind Side mode.
 * Palette entries 56..63 (48..55 are the UI theme's accent and secondary ramps). */
enum { FL_L0 = 56, FL_L1, FL_L2, FL_L3, FL_S0, FL_S1, FL_S2, FL_S3 };
static const uint32_t fl_back_rgb[8] = {
    0x130826, 0x261046, 0x4C186E, 0x94227A,   /* outrun: midnight purple -> royal violet -> neon plum -> hot magenta */
    0x081028, 0x102048, 0x183872, 0x2062AC,   /* cyber: abyssal navy -> deep cobalt -> electric blue -> cyan glow */
};

#endif
