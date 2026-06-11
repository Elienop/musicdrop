/**
 * The MusicDrop logo, ported from MusicDrop-old's `web/static/logo.svg` —
 * re-colored onto the design tokens instead of the original hardcoded
 * teal/black + prefers-color-scheme block: accent shapes fill with the
 * violet `--primary` family and letterforms with `--foreground`, so the
 * logo follows the color guide automatically (one accent, neutral ramp).
 *
 * Both render `aria-hidden` — they live inside links that carry the
 * accessible name ("MusicDrop").
 */

/** Full "[bars]usicDrop" lockup — expanded sidebar + mobile drawer. */
export function LogoWordmark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 415.31 97.49" aria-hidden="true" className={className}>
      {/* The translucent accent panel behind "Drop". */}
      <polyline
        className="fill-primary/40"
        points="413.31 0 223.7 0 223.7 91.82 413.31 91.82 413.31 0"
      />
      {/* "usic" */}
      <g className="fill-foreground">
        <path d="M92.88,78.76v-6.83c-3.36,5.54-8.41,8.11-15.14,8.11-9.7,0-16.53-7.22-16.53-17.91V30.08h12.86v30.08c0,6.13,2.77,9.1,8.21,9.1,6.53,0,9.99-4.65,9.99-11.08v-28.1h12.76v48.69h-12.17Z" />
        <path d="M139.98,44.82c-.69-4.75-3.27-6.43-9.1-6.43-4.85,0-7.62,1.19-7.62,4.06s2.67,4.06,7.92,5.54c5.54,1.58,10.79,2.67,14.55,4.16,5.15,2.08,8.01,5.44,8.01,11.78,0,10.09-7.42,16.13-21.27,16.13-14.94,0-23.25-7.03-23.45-16.92h13.26c0,4.55,3.86,7.22,10.09,7.22,4.55,0,8.61-1.39,8.61-5.05,0-3.46-3.66-4.55-7.82-5.54-8.21-1.98-12.76-3.36-16.23-5.54-4.55-2.87-6.13-6.63-6.13-10.98,0-8.21,5.64-14.45,20.48-14.45,14.05,0,20.38,5.54,21.08,16.03h-12.37Z" />
        <path d="M157.7,24.24v-11.97h12.86v11.97h-12.86ZM157.7,78.76V30.08h12.86v48.69h-12.86Z" />
        <path d="M220.24,61.15c-1.39,11.28-10.59,18.9-22.36,18.9-13.26,0-22.36-8.81-22.36-26.22s9.1-25.04,22.76-25.04c12.67,0,21.28,7.22,22.07,18.7h-12.96c-.69-5.05-4.35-8.02-9.2-8.02-5.44,0-9.9,3.46-9.9,14.05s4.45,15.73,9.5,15.73,9-2.87,9.5-8.11h12.96Z" />
      </g>
      {/* The three equalizer bars — the brand mark. */}
      <g className="fill-primary">
        <rect y="12.27" width="15.56" height="66.5" />
        <rect x="20.18" y="50.49" width="15.56" height="28.27" />
        <rect x="40.35" y="30.08" width="15.56" height="48.67" />
      </g>
      {/* "Drop" */}
      <g className="fill-foreground">
        <path d="M223.7,78.76V13.06h25.43c17.81,0,30.48,12.17,30.48,32.16s-11.38,33.54-28.4,33.54h-27.51ZM248.44,67.58c11.97,0,17.12-7.52,17.12-22.36s-5.15-20.78-18.31-20.78h-10.19v43.14h11.38Z" />
        <path d="M284.07,78.76V30.08h12.07v5.84c4.25-6.53,9.2-7.12,14.45-7.12h1.68v13.16c-1.19-.2-2.38-.3-3.56-.3-7.92,0-11.78,3.96-11.78,11.78v25.33h-12.86Z" />
        <path d="M311.78,54.42c0-15.44,9.6-25.63,25.04-25.63s24.74,10.09,24.74,25.63-9.6,25.63-24.74,25.63-25.04-10.49-25.04-25.63ZM348.59,54.42c0-9.9-3.96-14.74-11.78-14.74s-11.78,4.85-11.78,14.74,3.96,14.84,11.78,14.84,11.78-4.95,11.78-14.84Z" />
        <path d="M366.2,97.49V30.08h12.37l.1,5.94c3.07-4.95,7.62-7.22,13.56-7.22,12.27,0,21.08,9.7,21.08,26.12,0,14.74-7.42,25.13-19.99,25.13-6.04,0-10.79-2.47-14.45-7.62v25.06h-12.67ZM400.24,54.03c0-8.71-4.55-14.74-10.98-14.74s-10.88,5.74-10.88,13.95c0,10.59,3.66,15.83,10.79,15.83,7.52,0,11.08-5.05,11.08-15.04Z" />
      </g>
      <rect className="fill-foreground" x="413.31" width="2" height="91.82" />
    </svg>
  );
}

/** Just the three bars — the compact mark for the collapsed rail. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 55.91 66.5" aria-hidden="true" className={className}>
      <g className="fill-primary">
        <rect y="0" width="15.56" height="66.5" />
        <rect x="20.18" y="38.22" width="15.56" height="28.27" />
        <rect x="40.35" y="17.81" width="15.56" height="48.67" />
      </g>
    </svg>
  );
}
