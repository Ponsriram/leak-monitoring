/**
 * Canonical country name -> an approximate centroid and ISO 3166-1 alpha-2 code.
 *
 * The leaks table stores no coordinates — only `victimCountry`, a canonical country name the
 * intel engine's gazetteer writes ("Germany", "United States", …). This is the frontend-only
 * bridge that lets the map plot those names and show a flag for them, and nothing more: it
 * does not change how the country is derived, only how it is presented.
 *
 * The keys here are kept in lockstep with the canonical names in
 * `services/intel/intel/extract/gazetteer.py` (the keys of COUNTRY_ALIASES / values of
 * CCTLD_COUNTRY). A name the engine can emit but that is missing here simply won't be
 * plotted — MapPage surfaces that count separately rather than dropping it silently.
 *
 * Coordinates are rough country centroids, adequate for a country-level dot map. They are not
 * survey-grade and are not meant to be.
 */
type CountryInfo = { coords: [number, number]; iso2: string };

export const COUNTRY_INFO: Record<string, CountryInfo> = {
  // North America
  "United States": { coords: [39.8, -98.6], iso2: "US" },
  Canada: { coords: [56.1, -106.3], iso2: "CA" },
  Mexico: { coords: [23.6, -102.6], iso2: "MX" },
  Guatemala: { coords: [15.8, -90.2], iso2: "GT" },
  "Costa Rica": { coords: [9.7, -83.8], iso2: "CR" },
  Panama: { coords: [8.5, -80.8], iso2: "PA" },
  "Dominican Republic": { coords: [18.7, -70.2], iso2: "DO" },
  "Puerto Rico": { coords: [18.2, -66.5], iso2: "PR" },

  // South America
  Brazil: { coords: [-14.2, -51.9], iso2: "BR" },
  Argentina: { coords: [-38.4, -63.6], iso2: "AR" },
  Chile: { coords: [-35.7, -71.5], iso2: "CL" },
  Colombia: { coords: [4.6, -74.3], iso2: "CO" },
  Peru: { coords: [-9.2, -75.0], iso2: "PE" },
  Ecuador: { coords: [-1.8, -78.2], iso2: "EC" },
  Uruguay: { coords: [-32.5, -55.8], iso2: "UY" },
  Paraguay: { coords: [-23.4, -58.4], iso2: "PY" },
  Bolivia: { coords: [-16.3, -63.6], iso2: "BO" },
  Venezuela: { coords: [6.4, -66.6], iso2: "VE" },

  // Western & Central Europe
  "United Kingdom": { coords: [54.0, -2.4], iso2: "GB" },
  Ireland: { coords: [53.4, -8.2], iso2: "IE" },
  Germany: { coords: [51.2, 10.4], iso2: "DE" },
  France: { coords: [46.2, 2.2], iso2: "FR" },
  Italy: { coords: [41.9, 12.6], iso2: "IT" },
  Spain: { coords: [40.0, -3.7], iso2: "ES" },
  Portugal: { coords: [39.4, -8.2], iso2: "PT" },
  Netherlands: { coords: [52.1, 5.3], iso2: "NL" },
  Belgium: { coords: [50.5, 4.5], iso2: "BE" },
  Luxembourg: { coords: [49.8, 6.1], iso2: "LU" },
  Switzerland: { coords: [46.8, 8.2], iso2: "CH" },
  Austria: { coords: [47.5, 14.6], iso2: "AT" },
  Denmark: { coords: [56.3, 9.5], iso2: "DK" },
  Sweden: { coords: [60.1, 18.6], iso2: "SE" },
  Norway: { coords: [60.5, 8.5], iso2: "NO" },
  Finland: { coords: [61.9, 25.7], iso2: "FI" },
  Iceland: { coords: [64.9, -19.0], iso2: "IS" },

  // Eastern & Southern Europe
  Poland: { coords: [51.9, 19.1], iso2: "PL" },
  Czechia: { coords: [49.8, 15.5], iso2: "CZ" },
  Slovakia: { coords: [48.7, 19.7], iso2: "SK" },
  Hungary: { coords: [47.2, 19.5], iso2: "HU" },
  Romania: { coords: [45.9, 24.9], iso2: "RO" },
  Bulgaria: { coords: [42.7, 25.5], iso2: "BG" },
  Greece: { coords: [39.1, 21.8], iso2: "GR" },
  Croatia: { coords: [45.1, 15.2], iso2: "HR" },
  Slovenia: { coords: [46.2, 14.8], iso2: "SI" },
  Serbia: { coords: [44.0, 21.0], iso2: "RS" },
  "Bosnia and Herzegovina": { coords: [43.9, 17.7], iso2: "BA" },
  Albania: { coords: [41.2, 20.2], iso2: "AL" },
  "North Macedonia": { coords: [41.6, 21.7], iso2: "MK" },
  Montenegro: { coords: [42.7, 19.4], iso2: "ME" },
  Estonia: { coords: [58.6, 25.0], iso2: "EE" },
  Latvia: { coords: [56.9, 24.6], iso2: "LV" },
  Lithuania: { coords: [55.2, 23.9], iso2: "LT" },
  Ukraine: { coords: [48.4, 31.2], iso2: "UA" },
  Belarus: { coords: [53.7, 27.9], iso2: "BY" },
  Moldova: { coords: [47.4, 28.4], iso2: "MD" },
  Cyprus: { coords: [35.1, 33.4], iso2: "CY" },
  Malta: { coords: [35.9, 14.4], iso2: "MT" },

  // Eurasia / Middle East
  Russia: { coords: [61.5, 105.3], iso2: "RU" },
  Armenia: { coords: [40.1, 45.0], iso2: "AM" },
  Azerbaijan: { coords: [40.1, 47.6], iso2: "AZ" },
  Kazakhstan: { coords: [48.0, 66.9], iso2: "KZ" },
  Uzbekistan: { coords: [41.4, 64.6], iso2: "UZ" },
  Turkey: { coords: [39.0, 35.2], iso2: "TR" },
  Israel: { coords: [31.0, 34.9], iso2: "IL" },
  "Saudi Arabia": { coords: [23.9, 45.1], iso2: "SA" },
  "United Arab Emirates": { coords: [23.4, 53.8], iso2: "AE" },
  Qatar: { coords: [25.4, 51.2], iso2: "QA" },
  Kuwait: { coords: [29.3, 47.5], iso2: "KW" },
  Bahrain: { coords: [26.1, 50.6], iso2: "BH" },
  Oman: { coords: [21.5, 55.9], iso2: "OM" },
  Jordan: { coords: [30.6, 36.2], iso2: "JO" },
  Lebanon: { coords: [33.9, 35.9], iso2: "LB" },

  // Africa
  Egypt: { coords: [26.8, 30.8], iso2: "EG" },
  Morocco: { coords: [31.8, -7.1], iso2: "MA" },
  Algeria: { coords: [28.0, 1.7], iso2: "DZ" },
  Tunisia: { coords: [33.9, 9.6], iso2: "TN" },
  Libya: { coords: [26.3, 17.2], iso2: "LY" },
  Nigeria: { coords: [9.1, 8.7], iso2: "NG" },
  Ghana: { coords: [7.9, -1.0], iso2: "GH" },
  Kenya: { coords: [-0.0, 37.9], iso2: "KE" },
  Tanzania: { coords: [-6.4, 34.9], iso2: "TZ" },
  Uganda: { coords: [1.4, 32.3], iso2: "UG" },
  Ethiopia: { coords: [9.1, 40.5], iso2: "ET" },
  "South Africa": { coords: [-30.6, 22.9], iso2: "ZA" },
  Zimbabwe: { coords: [-19.0, 29.2], iso2: "ZW" },
  Zambia: { coords: [-13.1, 27.8], iso2: "ZM" },
  Botswana: { coords: [-22.3, 24.7], iso2: "BW" },
  Namibia: { coords: [-22.6, 18.5], iso2: "NA" },
  Mozambique: { coords: [-18.7, 35.5], iso2: "MZ" },
  Angola: { coords: [-11.2, 17.9], iso2: "AO" },
  Senegal: { coords: [14.5, -14.5], iso2: "SN" },
  "Ivory Coast": { coords: [7.5, -5.5], iso2: "CI" },
  Cameroon: { coords: [7.4, 12.4], iso2: "CM" },

  // South & East Asia
  India: { coords: [22.4, 78.7], iso2: "IN" },
  Pakistan: { coords: [30.4, 69.3], iso2: "PK" },
  Bangladesh: { coords: [23.7, 90.4], iso2: "BD" },
  "Sri Lanka": { coords: [7.9, 80.8], iso2: "LK" },
  Nepal: { coords: [28.4, 84.1], iso2: "NP" },
  China: { coords: [35.9, 104.2], iso2: "CN" },
  "Hong Kong": { coords: [22.3, 114.2], iso2: "HK" },
  Taiwan: { coords: [23.7, 121.0], iso2: "TW" },
  Japan: { coords: [36.2, 138.3], iso2: "JP" },
  "South Korea": { coords: [36.5, 127.9], iso2: "KR" },
  Singapore: { coords: [1.35, 103.8], iso2: "SG" },
  Malaysia: { coords: [4.2, 101.9], iso2: "MY" },
  Indonesia: { coords: [-2.5, 118.0], iso2: "ID" },
  Thailand: { coords: [15.9, 101.0], iso2: "TH" },
  Vietnam: { coords: [14.1, 108.3], iso2: "VN" },
  Philippines: { coords: [12.9, 121.8], iso2: "PH" },
  Cambodia: { coords: [12.6, 104.9], iso2: "KH" },
  Myanmar: { coords: [21.9, 95.9], iso2: "MM" },

  // Oceania
  Australia: { coords: [-25.3, 133.8], iso2: "AU" },
  "New Zealand": { coords: [-41.8, 173.0], iso2: "NZ" },
};

/** The centroid for a canonical country name, or null when we have no coordinate for it. */
export function coordsFor(country: string): [number, number] | null {
  return COUNTRY_INFO[country]?.coords ?? null;
}

/**
 * The flag emoji for a canonical country name, from its ISO code turned into the two regional
 * indicator symbols. Empty string when the country is unknown, so callers can render nothing
 * rather than a tofu box.
 */
export function flagFor(country: string): string {
  const iso2 = COUNTRY_INFO[country]?.iso2;
  if (!iso2) return "";
  return String.fromCodePoint(
    ...[...iso2].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65),
  );
}
