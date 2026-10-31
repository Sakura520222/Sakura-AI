/** Keep the original Sakura AI brand mark used by the existing WebUI. */
export function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <span
      className="sakura-brand-mark"
      style={{ fontSize: size }}
      aria-hidden="true"
    >
      🌸
    </span>
  );
}
