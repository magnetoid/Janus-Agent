import { useGpuTier } from "@/hooks/use-gpu-tier";
import { useTheme } from "@/themes";

/** True for `transparent` and any `rgba(…, 0)` / `#rrggbb00` value. */
function isInvisible(color: string): boolean {
  const c = color.trim().toLowerCase();
  return (
    c === "" ||
    c === "transparent" ||
    c === "none" ||
    /,\s*0(\.0+)?\s*\)$/.test(c) ||
    /^#[0-9a-f]{6}00$/.test(c)
  );
}

/**
 * Optional full-viewport effects a theme can switch on. The canvas color
 * itself is painted by `body`; a theme that asks for none of these (the
 * default) renders nothing here, so there are no blended layers to
 * composite on every frame.
 *
 *   z-0    `assets.bg` — the theme's background image, under the UI
 *   z-0    `palette.warmGlow` — a soft vignette toward the far corner
 *   z-200  `palette.foreground` — the inversion "lens": an opaque layer
 *          in `difference` mode that flips everything beneath it, which
 *          is how a theme authored on a dark canvas becomes a light one.
 *          Above every portaled overlay (menus, dialogs, tooltips) so
 *          those are flipped with the rest of the page.
 *   z-201  `palette.noiseOpacity` — film grain; skipped without a GPU or
 *          when the user prefers reduced motion.
 */
export function Backdrop() {
  const { theme } = useTheme();
  const gpuTier = useGpuTier();

  const hasImage = Boolean(theme.assets?.bg?.trim());
  const hasGlow = !isInvisible(theme.palette.warmGlow);
  const inverted = theme.palette.foreground.alpha > 0;
  const hasNoise = theme.palette.noiseOpacity > 0 && gpuTier > 0;

  return (
    <>
      {hasImage && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-0"
          style={
            {
              backgroundImage: "var(--theme-asset-bg)",
              backgroundSize: "var(--component-backdrop-background-size, cover)",
              backgroundPosition:
                "var(--component-backdrop-background-position, center)",
              mixBlendMode:
                "var(--component-backdrop-filler-blend-mode, normal)",
              opacity: "var(--component-backdrop-filler-opacity, 0.08)",
            } as unknown as React.CSSProperties
          }
        />
      )}

      {hasGlow && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-0"
          style={{
            background:
              "radial-gradient(ellipse at 0% 0%, transparent 60%, var(--warm-glow) 100%)",
            mixBlendMode: "lighten",
            opacity: 0.22,
          }}
        />
      )}

      {inverted && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0"
          style={{
            backgroundColor: "var(--foreground)",
            mixBlendMode: "difference",
            zIndex: 200,
          }}
        />
      )}

      {hasNoise && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0"
          style={{
            backgroundImage:
              "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' fill='%23eaeaea' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E\")",
            backgroundSize: "512px 512px",
            mixBlendMode: "soft-light",
            opacity: "calc(0.3 * var(--noise-opacity-mul, 1))",
            zIndex: 201,
          }}
        />
      )}
    </>
  );
}
