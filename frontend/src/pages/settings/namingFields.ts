// frontend/src/pages/settings/namingFields.ts
/** Curated, documented beets path-format tokens for the Insert palette. */
export interface NamingToken {
  insert: string;
  label: string;
  hint: string;
}

export const NAMING_FIELDS: NamingToken[] = [
  { insert: "$albumartist", label: "$albumartist", hint: "Album artist" },
  { insert: "$album", label: "$album", hint: "Album title" },
  { insert: "$artist", label: "$artist", hint: "Track artist" },
  { insert: "$title", label: "$title", hint: "Track title" },
  { insert: "$track", label: "$track", hint: "Track number (zero-padded)" },
  { insert: "$tracktotal", label: "$tracktotal", hint: "Number of tracks" },
  { insert: "$disc", label: "$disc", hint: "Disc number" },
  { insert: "$disctotal", label: "$disctotal", hint: "Number of discs" },
  { insert: "$year", label: "$year", hint: "Release year" },
  {
    insert: "$original_year",
    label: "$original_year",
    hint: "Original release year",
  },
  { insert: "$genre", label: "$genre", hint: "Genre" },
  {
    insert: "$albumtype",
    label: "$albumtype",
    hint: "Release type (album, single…)",
  },
  { insert: "$label", label: "$label", hint: "Record label" },
  { insert: "$format", label: "$format", hint: "File format (FLAC, MP3…)" },
];

export const NAMING_FUNCTIONS: NamingToken[] = [
  {
    insert: "%aunique{}",
    label: "%aunique{}",
    hint: "Disambiguate same-named albums",
  },
  { insert: "%if{,,}", label: "%if{cond,then,else}", hint: "Conditional text" },
  {
    insert: "%the{}",
    label: "%the{text}",
    hint: "Move leading 'The' to the end",
  },
  { insert: "%upper{}", label: "%upper{text}", hint: "Uppercase" },
  { insert: "%lower{}", label: "%lower{text}", hint: "Lowercase" },
  { insert: "%title{}", label: "%title{text}", hint: "Title Case" },
  { insert: "%left{,}", label: "%left{text,n}", hint: "First n characters" },
  { insert: "%right{,}", label: "%right{text,n}", hint: "Last n characters" },
  { insert: "%asciify{}", label: "%asciify{text}", hint: "Convert to ASCII" },
  {
    insert: "%first{}",
    label: "%first{text}",
    hint: "First value of a multi-value field",
  },
];
