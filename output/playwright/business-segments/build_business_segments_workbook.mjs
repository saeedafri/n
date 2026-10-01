import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const supportDir = path.resolve("output/playwright/business-segments");
const sourcePath = path.join(supportDir, "staging_business_segments.json");
const outputDir = path.resolve("outputs/business-segments-stg-2026-09-30");
const outputPath = path.join(outputDir, "Business_Segments_STG_Review.xlsx");
const previewDir = path.join(supportDir, "previews");
const data = JSON.parse(await fs.readFile(sourcePath, "utf8"));

const workbook = Workbook.create();
const overview = workbook.worksheets.add("Overview");
const dropdown = workbook.worksheets.add("Current Dropdown");
const allLabels = workbook.worksheets.add("All Business Labels");
const locationReview = workbook.worksheets.add("Location Review");

const fontFamily = "Arial";
const navy = "#17365D";
const blue = "#1F4E78";
const lightBlue = "#D9EAF7";
const paleBlue = "#EAF3F8";
const amber = "#FFF2CC";
const lightRed = "#FCE4D6";
const lightGray = "#F2F2F2";
const border = "#D9E2F3";
const darkText = "#1F2937";

function boolText(value) {
  return value ? "Yes" : "No";
}

function metric(row, name) {
  return boolText(row[`has_${name}`]);
}

function setBase(sheet, usedRange) {
  sheet.showGridLines = false;
  sheet.getRange(usedRange).format.font = { name: fontFamily, size: 10, color: darkText };
  sheet.getRange(usedRange).format.verticalAlignment = "center";
}

function setTitle(sheet, title, subtitle, endCol) {
  sheet.getRange(`A2:${endCol}2`).merge();
  sheet.getRange("A2").values = [[title]];
  sheet.getRange("A2").format.font = { name: fontFamily, size: 14, bold: true, color: navy };
  sheet.getRange(`A3:${endCol}3`).merge();
  sheet.getRange("A3").values = [[subtitle]];
  sheet.getRange("A3").format.font = { name: fontFamily, size: 10, italic: true, color: "#5B6573" };
  sheet.getRange(`A4:${endCol}4`).format.borders = {
    bottom: { style: "thin", color: blue },
  };
}

function styleHeader(range) {
  range.format.fill = navy;
  range.format.font = { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" };
  range.format.horizontalAlignment = "center";
  range.format.verticalAlignment = "center";
  range.format.wrapText = true;
  range.format.rowHeight = 36;
  range.format.borders = {
    insideVertical: { style: "thin", color: "#FFFFFF" },
    bottom: { style: "thin", color: "#FFFFFF" },
  };
}

const totalLabels = data.total_distinct_business_labels;
const dropdownRows = data.rows.filter((row) => row.shown_in_current_dropdown);
const locationRows = data.rows.filter((row) => row.location_like);
const locationInDropdown = locationRows.filter((row) => row.shown_in_current_dropdown).length;
const exactGeoOverlap = locationRows.filter((row) => row.also_in_geo_cache_exact_label).length;
const valuesOnly = data.rows.filter((row) => row.source_coverage === "Values cache only").length;
const memberOnly = data.rows.filter((row) => row.source_coverage === "Member cache only").length;

setBase(overview, "A1:H31");
overview.tabColor = navy;
setTitle(
  overview,
  "Business Segments review",
  "STG Company Screening > Financial Information > Business Segments | Accessed 30 Sep 2026",
  "H",
);

overview.getRange("A6:B6").values = [["Scope", "Count"]];
overview.getRange("A7:B13").values = [
  ["Current dropdown options", dropdownRows.length],
  ["Member-cache labels", data.total_member_cache_rows],
  ["Distinct labels across staging caches", totalLabels],
  ["Location-like labels", locationRows.length],
  ["Location-like labels in current dropdown", locationInDropdown],
  ["Exact labels also in geographical cache", exactGeoOverlap],
  ["Companies with cached business-segment values", Number(data.meta.distinct_tickers || 0)],
];
styleHeader(overview.getRange("A6:B6"));
overview.getRange("B7:B13").format.numberFormat = "#,##0";
overview.getRange("A7:B13").format.borders = {
  bottom: { style: "thin", color: border },
};

overview.getRange("D6:H6").merge();
overview.getRange("D6").values = [["Why geographical names appear under Business Segments"]];
overview.getRange("D6:H6").format.fill = blue;
overview.getRange("D6:H6").format.font = { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" };
overview.getRange("D7:H11").merge();
overview.getRange("D7").values = [[
  "Routing follows the XBRL section or axis supplied by the filer, not the words in the label. A company may define North America, Europe or International as an operating business segment. Business-axis labels are preserved as filed; only geographic-axis labels are canonicalized and filtered. The cache presets also include Asia and APAC Segment as business labels. Review location-like names before reassigning them because moving every place name would remove valid operating segments.",
]];
overview.getRange("D7:H11").format.wrapText = true;
overview.getRange("D7:H11").format.verticalAlignment = "top";
overview.getRange("D7:H11").format.fill = paleBlue;
overview.getRange("D7:H11").format.borders = { preset: "outside", style: "thin", color: border };

overview.getRange("A16:H16").merge();
overview.getRange("A16").values = [["How to use this workbook"]];
overview.getRange("A16:H16").format.fill = blue;
overview.getRange("A16:H16").format.font = { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" };
overview.getRange("A17:H20").values = [
  ["Current Dropdown", "Exact 500 options returned by the portal, ordered by company count.", null, null, null, null, null, null],
  ["All Business Labels", `Union of the ${data.total_member_cache_rows.toLocaleString()} member-cache labels and ${Number(data.meta.distinct_business_members || 0).toLocaleString()} values-cache labels.`, null, null, null, null, null, null],
  ["Location Review", "Location-like candidates with editable Decision, Final Assignment and Reviewer Notes fields.", null, null, null, null, null, null],
  ["Location-like", "A review flag based on the approved geo map, exact geo-cache overlap or the repository heuristic. It is not an automatic reassignment.", null, null, null, null, null, null],
];
for (let row = 17; row <= 20; row += 1) {
  overview.getRange(`B${row}:H${row}`).merge();
}
overview.getRange("A17:A20").format.font = { name: fontFamily, size: 10, bold: true, color: navy };
overview.getRange("A17:H20").format.borders = { bottom: { style: "thin", color: border } };
overview.getRange("B17:H20").format.wrapText = true;

overview.getRange("A23:B23").values = [["Portal behavior", "Value"]];
overview.getRange("A24:B29").values = [
  ["Dropdown sort", "Company count descending, then label ascending"],
  ["Dropdown row limit", data.dropdown_limit],
  ["Supported metrics", data.metric_options.join(", ")],
  ["Cached value rows", Number(data.meta.total_value_rows || 0)],
  ["Fiscal-year coverage", `${data.meta.earliest_year} to ${data.meta.latest_year}`],
  ["Values-cache updated", String(data.meta.values_updated_at || "")],
];
styleHeader(overview.getRange("A23:B23"));
overview.getRange("A24:B29").format.borders = { bottom: { style: "thin", color: border } };
overview.getRange("B25:B27").format.numberFormat = "#,##0";
overview.getRange("A31:H31").merge();
overview.getRange("A31").values = [["Source: Coresight STG portal and staging screening segment caches, accessed through the repository's existing local authentication bypass."]];
overview.getRange("A31").format.font = { name: fontFamily, size: 9, italic: true, color: "#5B6573" };
overview.getRange("A31").format.wrapText = true;
overview.getRange("A1:H31").format.columnWidth = 15;
overview.getRange("A1:A31").format.columnWidth = 33;
overview.getRange("B1:B31").format.columnWidth = 24;
overview.getRange("C1:C31").format.columnWidth = 3;
overview.getRange("D1:H31").format.columnWidth = 16;
overview.getRange("D7:H11").format.rowHeight = 26;

const commonHeaders = [
  "Dropdown Rank",
  "In Current Dropdown",
  "Business Segment",
  "Source Coverage",
  "Member Cache Companies",
  "Values Cache Companies",
  "Cached Value Rows",
  "Earliest FY",
  "Latest FY",
  "Sample Companies",
  "Revenues",
  "Operating Profit Before Tax",
  "Assets",
  "Depreciation & Amortization",
  "Capital Expenditure",
  "Location-like",
  "Location Review Reason",
  "Also in Geographical Cache",
  "Geo Cache Companies",
  "Cache Updated",
];

function commonRow(row) {
  return [
    row.dropdown_rank ?? null,
    boolText(row.shown_in_current_dropdown),
    row.business_segment,
    row.source_coverage,
    row.member_cache_company_count,
    row.values_cache_company_count,
    row.cached_value_rows,
    row.earliest_fiscal_year ?? null,
    row.latest_fiscal_year ?? null,
    row.sample_companies,
    metric(row, "Revenues"),
    metric(row, "Operating Profit Before Tax"),
    metric(row, "Assets"),
    metric(row, "Depreciation & Amortization"),
    metric(row, "Capital Expenditure"),
    boolText(row.location_like),
    row.location_review_reason,
    boolText(row.also_in_geo_cache_exact_label),
    row.geo_cache_company_count,
    row.cache_updated_at,
  ];
}

function buildDataSheet(sheet, title, subtitle, rows, tableName, tabColor) {
  const endRow = rows.length + 5;
  setBase(sheet, `A1:T${endRow}`);
  sheet.tabColor = tabColor;
  setTitle(sheet, title, subtitle, "T");
  sheet.getRange("A5:T5").values = [commonHeaders];
  sheet.getRange(`A6:T${endRow}`).values = rows.map(commonRow);
  styleHeader(sheet.getRange("A5:T5"));
  sheet.getRange(`A6:T${endRow}`).format.borders = { bottom: { style: "thin", color: "#E8EDF3" } };
  sheet.getRange(`A6:A${endRow}`).format.numberFormat = "#,##0";
  sheet.getRange(`E6:G${endRow}`).format.numberFormat = "#,##0";
  sheet.getRange(`H6:I${endRow}`).format.numberFormat = "0";
  sheet.getRange(`S6:S${endRow}`).format.numberFormat = "#,##0";
  sheet.getRange(`A5:T${endRow}`).format.verticalAlignment = "center";
  sheet.getRange(`J6:J${endRow}`).format.wrapText = true;
  sheet.getRange(`Q6:Q${endRow}`).format.wrapText = true;
  sheet.getRange(`P6:P${endRow}`).conditionalFormats.add("containsText", {
    text: "Yes",
    format: { fill: amber, font: { bold: true, color: "#7F6000" } },
  });
  sheet.getRange(`R6:R${endRow}`).conditionalFormats.add("containsText", {
    text: "Yes",
    format: { fill: lightRed, font: { bold: true, color: "#9C0006" } },
  });
  const table = sheet.tables.add(`A5:T${endRow}`, true, tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  sheet.freezePanes.freezeRows(5);
  sheet.freezePanes.freezeColumns(3);
  sheet.getRange(`A1:T${endRow}`).format.columnWidth = 12;
  sheet.getRange(`A1:A${endRow}`).format.columnWidth = 11;
  sheet.getRange(`B1:B${endRow}`).format.columnWidth = 13;
  sheet.getRange(`C1:C${endRow}`).format.columnWidth = 30;
  sheet.getRange(`D1:D${endRow}`).format.columnWidth = 20;
  sheet.getRange(`E1:I${endRow}`).format.columnWidth = 13;
  sheet.getRange(`J1:J${endRow}`).format.columnWidth = 52;
  sheet.getRange(`K1:O${endRow}`).format.columnWidth = 14;
  sheet.getRange(`P1:P${endRow}`).format.columnWidth = 12;
  sheet.getRange(`Q1:Q${endRow}`).format.columnWidth = 40;
  sheet.getRange(`R1:R${endRow}`).format.columnWidth = 15;
  sheet.getRange(`S1:S${endRow}`).format.columnWidth = 13;
  sheet.getRange(`T1:T${endRow}`).format.columnWidth = 19;
  return endRow;
}

const dropdownEnd = buildDataSheet(
  dropdown,
  "Current Business Segments dropdown",
  "Exact 500 options returned by STG, ordered by company count descending and label ascending",
  dropdownRows,
  "CurrentDropdownTable",
  blue,
);

const allEnd = buildDataSheet(
  allLabels,
  "All business-segment labels in staging caches",
  "Union of member-cache and values-cache labels; use In Current Dropdown to isolate the portal's current 500-option list",
  data.rows,
  "AllBusinessLabelsTable",
  "#5B9BD5",
);

const reviewHeaders = [
  "Dropdown Rank",
  "In Current Dropdown",
  "Business Segment",
  "Member Cache Companies",
  "Values Cache Companies",
  "Earliest FY",
  "Latest FY",
  "Sample Companies",
  "Location Review Reason",
  "Also in Geographical Cache",
  "Geo Cache Companies",
  "Revenues",
  "Operating Profit Before Tax",
  "Assets",
  "Depreciation & Amortization",
  "Capital Expenditure",
  "Decision",
  "Final Assignment",
  "Reviewer Notes",
];
const reviewEnd = locationRows.length + 5;
setBase(locationReview, `A1:S${reviewEnd}`);
locationReview.tabColor = "#ED7D31";
setTitle(
  locationReview,
  "Location-like business segments for team review",
  "Candidate list only. Filing-axis semantics can make a geographical name a valid operating business segment.",
  "S",
);
locationReview.getRange("A5:S5").values = [reviewHeaders];
locationReview.getRange(`A6:S${reviewEnd}`).values = locationRows.map((row) => [
  row.dropdown_rank ?? null,
  boolText(row.shown_in_current_dropdown),
  row.business_segment,
  row.member_cache_company_count,
  row.values_cache_company_count,
  row.earliest_fiscal_year ?? null,
  row.latest_fiscal_year ?? null,
  row.sample_companies,
  row.location_review_reason,
  boolText(row.also_in_geo_cache_exact_label),
  row.geo_cache_company_count,
  metric(row, "Revenues"),
  metric(row, "Operating Profit Before Tax"),
  metric(row, "Assets"),
  metric(row, "Depreciation & Amortization"),
  metric(row, "Capital Expenditure"),
  "",
  "",
  "",
]);
styleHeader(locationReview.getRange("A5:S5"));
locationReview.getRange(`A6:S${reviewEnd}`).format.borders = { bottom: { style: "thin", color: "#E8EDF3" } };
locationReview.getRange(`A6:A${reviewEnd}`).format.numberFormat = "#,##0";
locationReview.getRange(`D6:E${reviewEnd}`).format.numberFormat = "#,##0";
locationReview.getRange(`F6:G${reviewEnd}`).format.numberFormat = "0";
locationReview.getRange(`K6:K${reviewEnd}`).format.numberFormat = "#,##0";
locationReview.getRange(`H6:I${reviewEnd}`).format.wrapText = true;
locationReview.getRange(`Q6:S${reviewEnd}`).format.fill = amber;
locationReview.getRange(`Q6:Q${reviewEnd}`).dataValidation = {
  rule: {
    type: "list",
    values: ["Keep in Business", "Move to Geographical", "Keep in Both", "Remove", "Needs Research"],
  },
};
locationReview.getRange(`J6:J${reviewEnd}`).conditionalFormats.add("containsText", {
  text: "Yes",
  format: { fill: lightRed, font: { bold: true, color: "#9C0006" } },
});
const reviewTable = locationReview.tables.add(`A5:S${reviewEnd}`, true, "LocationReviewTable");
reviewTable.style = "TableStyleMedium2";
reviewTable.showFilterButton = true;
locationReview.freezePanes.freezeRows(5);
locationReview.freezePanes.freezeColumns(3);
locationReview.getRange(`A1:S${reviewEnd}`).format.columnWidth = 12;
locationReview.getRange(`A1:A${reviewEnd}`).format.columnWidth = 11;
locationReview.getRange(`B1:B${reviewEnd}`).format.columnWidth = 13;
locationReview.getRange(`C1:C${reviewEnd}`).format.columnWidth = 30;
locationReview.getRange(`D1:G${reviewEnd}`).format.columnWidth = 13;
locationReview.getRange(`H1:H${reviewEnd}`).format.columnWidth = 52;
locationReview.getRange(`I1:I${reviewEnd}`).format.columnWidth = 42;
locationReview.getRange(`J1:K${reviewEnd}`).format.columnWidth = 15;
locationReview.getRange(`L1:P${reviewEnd}`).format.columnWidth = 14;
locationReview.getRange(`Q1:Q${reviewEnd}`).format.columnWidth = 22;
locationReview.getRange(`R1:R${reviewEnd}`).format.columnWidth = 22;
locationReview.getRange(`S1:S${reviewEnd}`).format.columnWidth = 38;

workbook.recalculate();

await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const inspections = [];
for (const [sheetName, range] of [
  ["Overview", "A1:H31"],
  ["Current Dropdown", "A1:T20"],
  ["All Business Labels", "A1:T20"],
  ["Location Review", "A1:S20"],
]) {
  const check = await workbook.inspect({
    kind: "table",
    range: `${sheetName}!${range}`,
    include: "values,formulas",
    tableMaxRows: 20,
    tableMaxCols: 20,
    maxChars: 12000,
  });
  inspections.push({ sheetName, ndjson: check.ndjson });
  const preview = await workbook.render({ sheetName, range, scale: 1.25, format: "png" });
  await fs.writeFile(path.join(previewDir, `${sheetName.replaceAll(" ", "_")}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
  maxChars: 12000,
});

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

const savedBlob = await FileBlob.load(outputPath);
const reopened = await SpreadsheetFile.importXlsx(savedBlob);
const reopenedCheck = await reopened.inspect({
  kind: "sheet,table",
  include: "id,name",
  maxChars: 8000,
  tableMaxRows: 3,
  tableMaxCols: 6,
});

console.log(JSON.stringify({
  outputPath,
  sheetCount: 4,
  counts: {
    dropdownRows: dropdownRows.length,
    allLabels: data.rows.length,
    locationRows: locationRows.length,
    exactGeoOverlap,
    memberOnly,
    valuesOnly,
  },
  endRows: { dropdownEnd, allEnd, reviewEnd },
  formulaErrors: errors.ndjson,
  reopened: reopenedCheck.ndjson,
  inspections,
}, null, 2));
