// ISWE palette (from iswe_pp): `indigo` is remapped to the brand navy so existing
// bg-indigo-600 / text-indigo-600 accents pick up the theme; `iswe` names the raw colours.
module.exports = {
  content: ["./previewer/web/templates/**/*.html", "./previewer/web/**/*.py"],
  theme: {
    extend: {
      fontFamily: { sans: ["Manrope", "Segoe UI", "system-ui", "sans-serif"] },
      colors: {
        indigo: {
          50: "#eef2fa", 100: "#dbe3f3", 200: "#c3cee3", 300: "#9fb0d1", 400: "#6f86b3",
          500: "#4a6496", 600: "#213965", 700: "#16274a", 800: "#13213f", 900: "#0e1830", 950: "#0a1122",
        },
        iswe: {
          navy: "#213965", "navy-dark": "#16274a", red: "#F52205", "red-dark": "#c81a02",
          green: "#53E90B", cyan: "#00FEFE", yellow: "#F9FF01",
        },
      },
    },
  },
  plugins: [],
};
