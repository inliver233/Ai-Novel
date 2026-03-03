import clsx from "clsx";

type ProgressBarProps = {
  value: number;
  ariaLabel: string;
  min?: number;
  max?: number;
  className?: string;
  indicatorClassName?: string;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function ProgressBar({ value, ariaLabel, min = 0, max = 100, className, indicatorClassName }: ProgressBarProps) {
  const safeMax = max > min ? max : min + 1;
  const clamped = clamp(Number.isFinite(value) ? value : min, min, safeMax);
  const percent = ((clamped - min) / (safeMax - min)) * 100;

  return (
    <div
      className={clsx("h-2 w-full rounded-full bg-border/60", className)}
      role="progressbar"
      aria-label={ariaLabel}
      aria-valuemin={min}
      aria-valuemax={safeMax}
      aria-valuenow={Math.round(clamped)}
    >
      <div
        className={clsx(
          "h-full rounded-full bg-accent motion-safe:transition-[width] motion-safe:duration-atelier motion-safe:ease-atelier",
          indicatorClassName,
        )}
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}
