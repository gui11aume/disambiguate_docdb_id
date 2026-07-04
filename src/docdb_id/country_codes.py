"""Two-letter publication-authority codes recognised by DOCDB.

This module exposes a single constant, `VALID_CC`, containing the set of
two-letter country / regional-office codes that appear as the `country`
attribute on `<exch:exchange-document>` in EPO DOCDB XML.

The set is derived from the EPO Register's public coverage list
(https://register.epo.org/help?lng=en&topic=countrycodes), which itself
follows WIPO Standard ST.3. It includes regional and intergovernmental
authorities such as `EP` (European Patent Office), `WO` (WIPO/PCT),
`AP` (ARIPO), `EA` (Eurasian Patent Organisation), `OA` (OAPI), and
`GC` (Gulf Cooperation Council), in addition to country codes proper.

Codes for historical entities that still appear in older DOCDB records
are kept (`CS` Czechoslovakia, `DD` East Germany, `SU` USSR, `YU`
Yugoslavia/Serbia and Montenegro) so backfile ingest does not silently
drop pre-1993 publications.

Treat this list as an approximation of "what shows up in our data" rather
than a permanent specification: WIPO ST.3 is amended periodically, and
DOCDB coverage changes over time. If a future ingest surfaces an unknown
code, prefer extending the set here over forking copies elsewhere.
"""

from __future__ import annotations

VALID_CC: frozenset[bytes] = frozenset(
    {
        b"AL",
        b"AP",
        b"AR",
        b"AT",
        b"AU",
        b"BA",
        b"BE",
        b"BG",
        b"BR",
        b"CA",
        b"CH",
        b"CL",
        b"CN",
        b"CO",
        b"CR",
        b"CS",
        b"CU",
        b"CY",
        b"CZ",
        b"DD",
        b"DE",
        b"DK",
        b"DZ",
        b"EA",
        b"EC",
        b"EE",
        b"EG",
        b"EP",
        b"ES",
        b"FI",
        b"FR",
        b"GB",
        b"GC",
        b"GE",
        b"GR",
        b"GT",
        b"HK",
        b"HR",
        b"HU",
        b"ID",
        b"IE",
        b"IL",
        b"IN",
        b"IS",
        b"IT",
        b"JP",
        b"KE",
        b"KR",
        b"LI",
        b"LT",
        b"LU",
        b"LV",
        b"MA",
        b"MC",
        b"MD",
        b"ME",
        b"MK",
        b"MN",
        b"MT",
        b"MW",
        b"MX",
        b"MY",
        b"NC",
        b"NI",
        b"NL",
        b"NO",
        b"NZ",
        b"OA",
        b"PA",
        b"PE",
        b"PH",
        b"PL",
        b"PT",
        b"RO",
        b"RS",
        b"RU",
        b"SE",
        b"SG",
        b"SI",
        b"SK",
        b"SM",
        b"SU",
        b"SV",
        b"TJ",
        b"TR",
        b"TT",
        b"TW",
        b"UA",
        b"US",
        b"UY",
        b"VN",
        b"WO",
        b"YU",
        b"ZA",
        b"ZM",
        b"ZW",
    }
)
