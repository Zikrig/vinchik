"""Render GIF: concentric distance rings colored by age band on each wave."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.patches import Circle, Wedge

RADIUS_SHOW = (10, 25, 50, 100, 250, 500)
AGE_BANDS = (
    ("±2", "#1F6F5B"),
    ("±5", "#2A9D8F"),
    ("±10", "#E9C46A"),
    ("любой", "#E76F51"),
)

# <100 km: fan out; ≥100: near the right axis, slightly offset
LABEL_ANGLES_DEG = {
    10: 22,
    25: 68,
    50: 118,
    100: 4,
    250: -10,
    500: 14,
}

OUT = Path(__file__).resolve().parents[1] / "data" / "docs" / "match_age_distance.gif"


def _ring_radius(km: float) -> float:
    return np.sqrt(km) * 1.15


def _wave_cells(wave: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for ri in range(len(RADIUS_SHOW)):
        ai = wave - ri
        if 0 <= ai < len(AGE_BANDS):
            out[ri] = ai
    return out


def _wrap_caption(wave: int | None, cells: dict[int, int]) -> str:
    if wave is None:
        return "Диагональ поиска\nцвет кольца = возраст"
    lines = [f"Волна {wave}"]
    for ri, ai in sorted(cells.items()):
        lines.append(f"{RADIUS_SHOW[ri]} км → {AGE_BANDS[ai][0]}")
    return "\n".join(lines)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    show_waves = 6
    frames: list[tuple[int | None, str]] = []
    frames.append((None, _wrap_caption(None, {})))
    for w in range(show_waves):
        cells = _wave_cells(w)
        cap = _wrap_caption(w, cells)
        frames.append((w, cap))
        frames.append((w, cap))
    frames.append((None, "Каждый показ\nзаново\n\nБлизкие своего\nвозраста — раньше"))

    fig = plt.figure(figsize=(9.0, 7.0), dpi=120)
    fig.patch.set_facecolor("#F7F3EC")
    ax_side = fig.add_axes([0.04, 0.06, 0.28, 0.88])
    ax = fig.add_axes([0.34, 0.06, 0.62, 0.88])

    max_r = _ring_radius(RADIUS_SHOW[-1]) + 1.8
    ring_rs = [_ring_radius(km) for km in RADIUS_SHOW]

    def draw(frame_i: int):
        ax.clear()
        ax_side.clear()

        wave, caption = frames[frame_i]
        cells = _wave_cells(wave) if wave is not None else {}

        # --- left column ---
        ax_side.set_facecolor("#F7F3EC")
        ax_side.set_xlim(0, 1)
        ax_side.set_ylim(0, 1)
        ax_side.axis("off")
        ax_side.text(
            0.0,
            0.98,
            caption,
            ha="left",
            va="top",
            fontsize=13,
            fontweight="bold",
            color="#1A1A1A",
            linespacing=1.45,
            transform=ax_side.transAxes,
        )
        ax_side.text(
            0.0,
            0.38,
            "цвет = возраст",
            ha="left",
            va="bottom",
            fontsize=10,
            color="#555",
            transform=ax_side.transAxes,
        )
        for i, (label, color) in enumerate(AGE_BANDS):
            y = 0.32 - i * 0.08
            ax_side.scatter(
                [0.06],
                [y],
                s=280,
                c=color,
                edgecolors="#264653",
                linewidths=0.9,
                transform=ax_side.transAxes,
                zorder=5,
                clip_on=False,
            )
            ax_side.text(
                0.16,
                y,
                label,
                ha="left",
                va="center",
                fontsize=13,
                color="#333",
                transform=ax_side.transAxes,
            )
        ax_side.text(
            1.0,
            0.0,
            f"{frame_i}/{len(frames) - 1}",
            ha="right",
            va="bottom",
            fontsize=9,
            color="#888",
            transform=ax_side.transAxes,
        )

        # --- rings ---
        ax.set_facecolor("#F7F3EC")
        ax.set_aspect("equal")
        ax.set_xlim(-max_r, max_r)
        ax.set_ylim(-max_r, max_r)
        ax.axis("off")

        prev = 0.0
        for i, (km, r) in enumerate(zip(RADIUS_SHOW, ring_rs)):
            if i in cells:
                color = AGE_BANDS[cells[i]][1]
                ax.add_patch(
                    Wedge(
                        (0, 0),
                        r,
                        0,
                        360,
                        width=r - prev,
                        facecolor=color,
                        edgecolor="#264653",
                        linewidth=1.2,
                        alpha=0.85,
                        zorder=i,
                    )
                )
            else:
                ax.add_patch(
                    Circle(
                        (0, 0),
                        r,
                        facecolor="none",
                        edgecolor="#C8BFB0",
                        linewidth=1.1,
                        zorder=i,
                    )
                )

            ang = np.radians(LABEL_ANGLES_DEG[km])
            pad = 0.75
            lx = (r + pad) * np.cos(ang)
            ly = (r + pad) * np.sin(ang)
            ha = "left" if np.cos(ang) >= -0.05 else "right"
            if np.sin(ang) > 0.4:
                va = "bottom"
            elif np.sin(ang) < -0.4:
                va = "top"
            else:
                va = "center"
            ax.text(
                lx,
                ly,
                f"{km} км",
                ha=ha,
                va=va,
                fontsize=9,
                color="#1A1A1A" if i in cells else "#777",
                fontweight="bold" if i in cells else "normal",
                zorder=30,
            )
            prev = r

        ax.scatter([0], [0], s=140, c="#264653", zorder=40, edgecolors="white", linewidths=2)
        ax.text(0, -1.55, "ты", ha="center", va="top", fontsize=11, color="#264653", zorder=41)
        return []

    anim = FuncAnimation(fig, draw, frames=len(frames), interval=850, blit=False)
    anim.save(OUT, writer=PillowWriter(fps=1.15))
    plt.close(fig)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
