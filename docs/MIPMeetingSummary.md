MIP Meeting Summary

SEC Screening, Executive Compensation, Form 8-K Analysis, and Data Pipeline Responsibilities

Scope

This document summarizes the MIP-related portion of the meeting only. All discussion related to CIP, consumer surveys, survey schedules, survey dashboards, survey logins, and CIP infrastructure has been excluded.

The discussion focused on improving MIP’s company and people screening capabilities, combining SEC and Yahoo Finance data, analyzing executive-management changes through Form 8-K Item 5.02, improving compensation comparisons, correcting filing and database-quality issues, and moving transformations from the UI into the data layer.

1. Executive Summary

The main objective is to improve MIP’s ability to provide a complete and usable view of companies, executives, compensation, filings, and management changes.

The key areas discussed were:

1. Improve company and people screening.
2. Combine SEC and Yahoo Finance executive data for US companies.
3. Support multi-year compensation analysis.
4. Show compensation data in a clearer year-by-year format.
5. Combine “Other” and “Unknown” executive categories.
6. Hide data-source and filing-form information from the standard published view.
7. Investigate whether executive changes can be extracted from Form 8-K Item 5.02.
8. Identify executive departures, appointments, resignations, promotions, and succession events.
9. Calculate executive tenure and role duration where reliable dates are available.
10. Eventually map executives moving between companies and compare compensation changes.
11. Correct data-quality problems affecting ASICS and BOO.
12. Ensure foreign-language filings are translated or replaced with English filings where available.
13. Move transformations and calculations from the UI into the data or server layer.
14. Preserve UI speed while expanding the amount of filing and compensation data.
15. Confirm with Shashank whether the data pipeline can support the required integrations.

The Form 8-K Item 5.02 initiative was discussed as an exploratory idea. The first step is to determine whether the filings contain sufficiently consistent wording and structure to support reliable extraction. If the approach is not technically reliable, it may be dropped or postponed.

2. Company Screening

2.1 Existing company-screening criteria

The company-screening area currently includes three main categories:

• Industry classification
• Country of incorporation
• Company information

Company information includes financial data and other company-level information that is available and considered sufficiently reliable.

The discussion indicated that company information may come from different sources, including:

• SEC filings
• Yahoo Finance
• Other available financial-data sources

The objective is to make the company screen useful without exposing unnecessary technical source details to end users.

2.2 Key developments

The key-development area has been separated from general company information and organized by development category.

The categories discussed include:

• Earnings guidance
• Press releases
• Restatements
• M&A activity
• Capital raising
• News-based developments
• Filing-based developments
• Other relevant company events

Key developments should also support date-range filtering, allowing users to specify the period in which they want to examine events.

2.3 Screen organization

The discussion included reorganizing the screening interface so that users can work with:

• Company key developments
• People
• Other relevant categories such as equities, fixed income, transactions, and projects

The objective is to make the major screening areas easier to find and logically grouped.

3. People Screening

3.1 People-screening attributes

The people-screening area includes:

• Industry classification
• Country of incorporation
• Role or title
• Executive name
• Compensation metrics
• Compensation year

The available executive titles discussed included:

• CEO
• CFO
• COO
• CIO
• CTO
• General counsel
• Other executive roles

The system may also contain records for people whose titles do not fit the predefined title categories.

3.2 Combining “Other” and “Unknown”

A specific UI change was requested: the “Other” and “Unknown” categories should be combined.

The reason is that both categories effectively represent records that do not fit into the main predefined executive-title categories.

“Unknown” may occur when compensation information is available but the source filing does not provide a usable title. The meeting specifically referred to records from SEC DEF14A-related material where compensation data existed but the title was not available.

The requested result is one category:

Other

rather than separate categories for:

• Other
• Unknown

This will reduce unnecessary segmentation and make the filter easier to understand.

4. Executive Compensation Metrics

4.1 Compensation fields discussed

The people-screening compensation metrics include:

• Salary
• Bonus
• Stock awards
• Option awards
• Non-equity incentive compensation
• All other compensation
• Total compensation
• Total pay
• Exercised value
• Unexercised value

These fields are drawn from different underlying datasets.

4.2 SEC compensation fields

The SEC-related executive-compensation data includes fields such as:

• Executive name
• Executive title
• Salary
• Bonus
• Stock awards
• Option awards
• Non-equity incentive compensation
• All other compensation
• Total compensation
• Filing form
• Filing year
• Filing or source reference

SEC data is primarily associated with executive compensation information contained in proxy statements and related filings.

4.3 Yahoo Finance compensation fields

Yahoo Finance provides a different set of executive fields, including:

• Executive name
• Executive title
• Year
• Age
• Year born
• Total pay
• Pay currency
• Exercised value
• Unexercised value

Some of these fields are not consistently available in SEC compensation data.

In particular, Yahoo Finance may provide:

• Age
• Year born
• Exercised value
• Unexercised value

where the SEC record is incomplete.

4.4 Only display selected metrics

During the review, salary was selected, but other fields such as bonus and stock awards still appeared.

This needs to be investigated and corrected.

The intended behavior is:

• If the user selects salary, show salary.
• If the user selects bonus, show bonus.
• If the user selects salary and stock awards, show those selected fields.
• Do not automatically display unrelated compensation metrics unless they were selected.

The filtering logic should ensure that the displayed columns correspond to the user’s selections.

5. Multi-Year Compensation Filtering

5.1 Multi-select year filtering

The manager requested that compensation year support multiple selections.

For example, a user should be able to select:

• 2024
• 2025
• 2026

or a range of available years.

The year filter should behave consistently with the multi-select filters already used elsewhere in the application.

5.2 Make multi-select obvious

Although multiple years could already be selected, the interface did not make this behavior obvious.

The user should be able to understand immediately that compensation years support multi-select.

Possible improvements include:

• A visible multi-select indicator
• A clearer label
• A selection control that visibly supports multiple values
• A short explanatory hint
• Consistent behavior with company and key-development filters

5.3 Years as dynamic columns

The current presentation was described as difficult to read.

When several years are selected, the years should become dynamic columns.

For example:

Executive	Title	Metric	2024	2025	2026
Executive A	CEO	Salary	Value	Value	Value

This would make it easier to compare compensation across years at a glance.

The year should not remain in a layout that forces users to scan multiple rows or interpret a cluttered result set.

5.4 Comparison objective

The purpose of the change is to support clearer comparisons, including:

• Salary changes across years
• Bonus changes across years
• Stock-award changes across years
• Total-compensation changes
• Compensation trends for the same executive
• Comparisons across executives
• Comparisons across companies

6. Combining SEC and Yahoo Finance Data

6.1 Current issue

The same US company may have data in both:

• SEC datasets
• Yahoo Finance datasets

However, the current implementation may use only one source for a given company or record.

This can create incomplete records.

For example:

• SEC may include salary and title.
• Yahoo Finance may include age and year born.
• SEC may not include exercised or unexercised value.
• Yahoo Finance may provide those fields.

If only SEC is used, the resulting record may contain unnecessary blanks.

6.2 Requested source strategy

The proposed source strategy was:

US companies

For US companies, run the process against:

• SEC
• Yahoo Finance

Then combine the results into one complete executive record where possible.

Non-US companies

For non-US companies, use Yahoo Finance where SEC data is not available or not applicable.

The objective is to provide the most complete available record without unnecessarily exposing source differences to the user.

6.3 Example

Walmart was discussed as an example.

If Walmart has SEC data and Yahoo Finance data, the system should attempt to combine them.

For instance:

• SEC provides executive compensation.
• Yahoo Finance provides age.
• The final output includes both, provided the records can be matched reliably.

The same approach should apply to other US companies with records in both systems.

6.4 Matching requirements

To combine records correctly, the system will need a matching strategy based on fields such as:

• Company identifier
• Ticker
• Executive name
• Normalized executive name
• Executive title
• Reporting year
• Other available identifiers

The matching process must avoid:

• Duplicate executives
• Incorrectly merging two different people
• Attaching one executive’s compensation to another person
• Overwriting a more reliable value with a less reliable value
• Creating contradictory records

6.5 Source-precedence rules

A source-precedence policy may be needed when both sources contain the same field.

For example, the system may need to decide:

• Whether SEC salary should override Yahoo Finance salary
• Whether Yahoo Finance age should fill only missing SEC age
• Whether the most recent value should be preferred
• Whether conflicting values should be retained for validation
• Whether values should have source confidence levels internally

These decisions were not finalized in the meeting and should be evaluated as part of the implementation.

6.6 Pipeline dependency

You need to confirm with Shashank whether the existing pipeline can:

1. Process SEC data.
2. Process Yahoo Finance data.
3. Run both sources for the same US company.
4. Match records between the sources.
5. Merge the available fields.
6. Handle missing values.
7. Avoid duplicate records.
8. Store the combined output efficiently.

Memory and storage were identified as potential constraints.

7. Data-Source and Filing-Form Visibility

7.1 Hide source information from standard output

The manager stated that the standard published view should not show where the data came from.

The following fields should not appear as normal user-facing columns:

• Data source
• Filing form
• Source-table information
• Internal extraction references

The user should see the business information rather than the technical origin of every value.

7.2 Possible secondary access

Source and filing details may still be useful internally.

The discussion left open the possibility of showing them only when:

• A user explicitly requests them
• An internal user needs to investigate a record
• A validation or audit view is opened
• A support or debugging function is used

However, the fields should not be displayed by default in the published interface.

7.3 Reason for hiding source details

The manager expressed concern that publishing the source and filing form would reveal where the company obtains the data.

Therefore, the standard MIP interface should present consolidated results without exposing the detailed source path.

8. Management Changes

8.1 Business objective

A major discussion point was the possibility of tracking executive-management changes.

The desired capability could identify:

• Executives leaving a company
• Executives joining a company
• Executives moving into new roles
• Promotions
• Demotions or lateral changes
• Succession events
• Interim appointments
• Changes in responsibilities
• Multiple titles held by one person
• Executive movement between competitors

8.2 Management-changes spreadsheet

The manager referred to a management-changes spreadsheet that included information such as:

• Date of change
• Company
• Previous position
• New position
• Executive
• Effective date
• Announced changes

The spreadsheet was used to illustrate how management changes could be represented.

8.3 Desired historical view

The desired output would show information such as:

• The executive’s earlier role
• How long the executive held that role
• When the executive changed positions
• The new position
• Whether the change represented a promotion or succession
• Whether the executive left the company
• Who replaced the executive

An example discussed involved an executive leaving the chief merchandising officer position and another person taking over the role.

The manager wanted the system to show something like:

Executive A held Position X from Year A to Year B and then moved to Position Y. Executive B assumed Position X effective Date Z.

8.4 Career and role history

A longer-term objective would be to show the executive’s progression, including:

• Start of employment
• Start of a particular role
• End of a particular role
• Time spent in each role
• Promotions
• Changes in responsibility
• Company departures
• Moves to another company

This would allow users to analyze executive experience and tenure.

9. Form 8-K Item 5.02

9.1 Why Item 5.02 is relevant

The discussion identified Form 8-K Item 5.02 as a promising source for management-change information.

Item 5.02 filings may describe:

• Departure of a director or executive officer
• Appointment of a new executive officer
• Resignation
• Retirement
• Termination
• Succession plans
• New responsibilities
• Changes in executive roles

The discussion observed that management-change information often appears under Item 5.02.

9.2 Example filing patterns

The examples discussed included wording such as:

• An executive will depart effective on a specified date.
• A company announced an executive’s resignation.
• A board approved a succession plan.
• An incoming executive will take over a role on a specified date.
• An executive informed the company of an intent to resign.
• A replacement executive was newly appointed.

These examples indicate that the information may contain both:

• A departing person
• An incoming or replacement person

9.3 Multiple titles

An executive may hold several titles simultaneously.

The meeting discussed an executive holding titles such as:

• Vice president
• Chief accounting officer
• Controller

The extraction logic must preserve all titles rather than selecting only one.

This is important because one person’s responsibilities may span several executive functions.

9.4 Potential fields to extract

The initial list of fields to evaluate includes:

• Company name
• Ticker
• Executive name
• Previous title
• Previous titles
• New title
• New titles
• Event type
• Resignation date
• Retirement date
• Departure date
• Appointment date
• Effective date
• Announcement date
• Previous company
• New company
• Replacement executive
• Reason for departure, if disclosed
• Filing date
• Filing form
• Item number
• Filing accession number
• Filing document link or identifier
• Extracted source text
• Confidence or validation status

9.5 Event types

Potential event classifications include:

• Resignation
• Retirement
• Departure
• Termination
• Appointment
• Promotion
• Succession
• Interim appointment
• Change in responsibilities
• Change in title
• Replacement
• Transfer
• Other management change

The extraction should distinguish between:

• The date the event was announced
• The date the event becomes effective
• The date the person actually leaves
• The date the replacement assumes the position

These dates may be different.

10. Proposed Item 5.02 Extraction Process

10.1 Initial exploratory phase

The first phase is not necessarily full production implementation.

The purpose is to determine whether Item 5.02 filings contain consistent enough patterns to support structured extraction.

The initial process should:

1. Collect representative Item 5.02 filings.
2. Review multiple companies and event types.
3. Compare wording and document structure.
4. Identify recurring phrases.
5. Identify common date formats.
6. Identify common title patterns.
7. Test whether incoming and outgoing executives can be separated.
8. Assess whether the extracted fields are reliable.

10.2 Use of regular expressions

Regular expressions may be useful for identifying:

• Dates
• Effective dates
• Names
• Titles
• Resignation language
• Appointment language
• Departure language
• Succession language

However, regular expressions alone may not be sufficient because filing language varies between companies.

The extraction process may require a combination of:

• Item number filtering
• Filing metadata
• Text pattern matching
• Named-entity recognition
• Title dictionaries
• Date normalization
• Context analysis
• Manual validation samples

10.3 Filing-specific wording

The same event may be described differently.

Examples include:

• “will depart the company effective…”
• “informed the company of her intent to resign…”
• “has resigned from her position…”
• “was appointed as…”
• “will succeed…”
• “will assume the role effective…”
• “the board approved a succession plan…”

The extraction logic should account for variations rather than relying on one exact phrase.

10.4 Determine whether a pattern is universal

The goal is to determine whether a universal extraction pattern can be established.

The meeting’s broader SEC-extraction discussion emphasized identifying reusable patterns across companies.

For Item 5.02, the questions are:

• Does the item consistently contain management-change information?
• Are titles described consistently?
• Are effective dates consistently stated?
• Can an outgoing and incoming executive be reliably linked?
• Can the reason for departure be extracted?
• Can role history be reconstructed?
• Can the same logic work across companies and industries?

10.5 Feasibility decision

After testing, there should be a clear conclusion:

If reliable

• Implement the extraction logic.
• Add the fields to the data pipeline.
• Create management-change records.
• Add appropriate filters.
• Calculate tenure where dates are available.

If unreliable

• Document the limitations.
• Identify which fields cannot be trusted.
• Avoid presenting speculative results.
• Defer or discontinue the feature.

The manager explicitly indicated that this was an idea to investigate, not an unconditional requirement.

11. Tenure and Experience Calculations

11.1 Intended calculations

If reliable appointment and departure dates can be extracted, the system could calculate:

• Time in a role
• Tenure at the company
• Years of executive experience
• Time between appointment and departure
• Time until succession
• Length of service before promotion
• Length of service before leaving
• Time in previous versus new role

11.2 Filing year is not a start date

The discussion clarified that filing year should not automatically be treated as the executive’s start year.

For example, the year associated with a DEF14A filing represents the year in which the filing was submitted or relates to the reporting period. It does not necessarily indicate when the executive joined the company or started the role.

Therefore, the system should not calculate tenure solely from:

• DEF14A filing year
• Compensation year
• Proxy-statement year

Instead, tenure should use:

• Appointment date
• Effective date
• Management-change date
• Employment start date
• Other reliable historical evidence

11.3 Date hierarchy

A possible date hierarchy could be:

1. Explicit effective date in an 8-K
2. Explicit appointment date
3. Explicit departure date
4. Employment start date from a reliable source
5. Historical filing evidence
6. Filing year only as a fallback indicator, not as a definitive start date

If the system only has a filing year, it should label the calculation as approximate or avoid calculating tenure.

12. Executive Movement Across Companies

12.1 Long-term concept

A longer-term idea was to track executives who move from one company to another, particularly competitors.

For example:

1. An executive appears in the original company’s DEF14A compensation data.
2. An Item 5.02 filing indicates that the executive is leaving.
3. The executive is identified at a new company.
4. The new company’s compensation and title are captured.
5. The system compares the executive’s prior and new positions.

12.2 Potential outputs

The product could show:

• Previous company
• Previous title
• Previous salary
• Previous total compensation
• New company
• New title
• New salary
• New total compensation
• Compensation increase or decrease
• Title change
• Date of move
• Approximate percentage change
• Whether the move was to a competitor

12.3 Salary mapping

The manager suggested that users may be interested in:

• What an executive earned at the original company
• What the executive earns at the new company
• How much compensation changed
• Whether the executive received a higher title
• Whether the executive moved into a more senior role
• How much the executive’s compensation increased

12.4 Matching challenge

This feature will require reliable person matching across companies.

Potential complications include:

• Name variations
• Middle initials
• Similar names
• Different title conventions
• Different currencies
• Different reporting years
• Different compensation definitions
• Missing birth-year data
• Companies using multiple names for the same person

This feature should be treated as a later phase after the basic management-change extraction is validated.

13. SEC Filings and IXBRL/XBRL

13.1 SEC filing structure

The meeting included a technical explanation of how SEC data is represented in filings.

SEC filings may include inline XBRL or IXBRL information. The filing contains:

• Tags
• Labels
• References
• Contexts
• Members
• Locations
• Values
• Filing sections

The system uses these elements to identify and display values.

13.2 Filing locations

The filing interface can link a displayed value back to its location in the filing.

When a value is selected, the user may be taken to the relevant location in the source document.

The system may visually identify the relevant tagged value within the filing.

13.3 XBRL tags

The tags follow a defined nomenclature.

There may be thousands of tags across SEC filings. The challenge is not simply finding the tag but understanding its context.

The same concept may appear under different contexts, including:

• Consolidated values
• Geographical values
• Operating-segment values
• Business-segment values
• Company-specific members
• Time periods
• Reporting dimensions

13.4 Context and classification

The extraction process must distinguish:

• What the tag represents
• Which company or entity it belongs to
• Which period it covers
• Whether it is annual or quarterly
• Whether it is consolidated
• Whether it is geographical
• Whether it belongs to a business segment
• Whether it is an operating segment

For example, revenue may be associated with:

• United States
• United Kingdom
• North America
• A particular business segment
• A consolidated company total

The value should not be categorized correctly based only on the label. The context must also be reviewed.

14. Segment and Geographic Data

14.1 Geographic segments

Geographic information may be represented through specific members or dimensions.

Examples discussed included:

• United States
• United Kingdom
• Other countries or regions

The system needs to identify that a value belongs to a geographic segment rather than to an operating or business segment.

14.2 Business segments

Business segments may use different members from geographic segments.

For example:

• North America may represent an operating segment.
• United States may represent a geographic segment.
• A product division may represent a business segment.

The extraction process should classify these separately.

14.3 Universal pattern approach

The current technical approach is to identify reusable patterns.

The process is:

1. Find the XBRL tag.
2. Identify the label.
3. Identify the context.
4. Identify the member.
5. Determine whether the value is consolidated, geographic, or operational.
6. Apply the appropriate business classification.
7. Store the transformed output.
8. Display it in the appropriate UI section.

However, some companies may file information differently. The system must support exceptions and validate results.

15. Filing Types

The meeting discussed several filing types.

15.1 10-K

The 10-K contains annual company information and financial disclosures.

The system may use 10-K data for:

• Annual financial statements
• Company information
• Business information
• Risk disclosures
• Employee counts
• Other annual disclosures

15.2 10-Q

The 10-Q provides quarterly information.

The system may use 10-Q data for:

• Quarterly financial statements
• Interim results
• Quarterly disclosures
• Updated financial information

15.3 DEF14A and proxy statements

DEF14A and related proxy materials may contain:

• Executive compensation
• Executive biographies
• Director information
• Corporate governance information
• Executive roles
• Compensation tables

The meeting noted that information historically associated with the 10-K may now be located in the proxy statement.

Therefore, the extraction logic may need to follow references from the 10-K to the relevant proxy filing.

15.4 Form 8-K

Form 8-K provides current event information.

For the MIP objective, Item 5.02 is particularly relevant because it may contain:

• Executive departures
• Appointments
• Resignations
• Succession announcements
• Changes in executive roles

15.5 Other filings

The discussion also referenced:

• Beneficial-ownership filings
• Statements of ownership
• Other priority SEC filings

The system should prioritize filings that contribute directly to the product rather than ingesting every available document without a clear purpose.

16. Filing Scope and Memory Constraints

16.1 Not every filing should be ingested automatically

The meeting emphasized that ingesting every filing can create large memory and processing requirements.

The system should prioritize:

• 10-K
• 10-Q
• DEF14A
• 8-K
• Other filings required for MIP features

The pipeline should avoid collecting unnecessary data merely because it is available.

16.2 Priority-based ingestion

A priority model could classify filings as:

High priority

• 10-K
• 10-Q
• DEF14A
• 8-K Item 5.02

Medium priority

• Other relevant proxy filings
• Ownership-related filings
• Filings needed to fill specific data gaps

Lower priority

• Filings that do not support a current MIP use case
• Duplicate documents
• Documents in unusable formats or languages

16.3 Storage implications

Before enabling both SEC and Yahoo Finance ingestion for all US companies, the team should evaluate:

• Storage requirements
• Pipeline execution time
• Memory consumption
• Duplicate records
• Incremental refresh behavior
• Processing frequency
• Error handling
• Query performance

17. Company Filings Interface

17.1 ER and document type

The discussion identified a relationship issue involving:

• ER
• Document type

The current interface may make it appear that the document type determines ER.

The actual relationship is the reverse:

ER determines the document type.

17.2 Requested UI change

The requested change is:

• Move ER to the front.
• Move document type to the end.

This will better reflect the underlying dependency.

17.3 Year and document type behavior

When the year changes, the available document type may change because the system derives the applicable document type from the available ER values.

The interface should avoid implying that document type independently controls ER.

17.4 Default document type

The default selection may currently reflect the latest available document type, such as a quarter.

The interface should ensure that this default behavior is understandable and consistent with the data model.

18. ASICS Data-Quality Issue

18.1 Problem observed

ASICS, ticker 7936, displayed incorrect or implausible years.

The displayed years extended into the future, including years such as:

• 2030
• 2031
• 2032
• 2033
• 2034
• 2035

This was identified as a data problem rather than merely a UI problem.

18.2 Likely storage problem

The discussion indicated that the incorrect years appeared to exist in the stored blob.

Therefore, simply modifying the interface would not be sufficient.

The underlying record or pipeline must be inspected.

18.3 Possible causes

Potential causes include:

• Incorrect fiscal-year values in the source data
• Incorrect mapping of years during ingestion
• Duplicate or repeated records
• Incorrect annual/interim classification
• Incorrect relationship between year and document type
• Incorrect source-table mapping
• Data accidentally written into the wrong table or blob
• Foreign-language filing ingestion problems
• Forecast years being mixed with historical years

18.4 Annual and quarterly distinction

The discussion clarified:

• SEC reporting generally includes Q1, Q2, and Q3 quarterly reports.
• Q4 is generally represented by the 10-K.

This should be accounted for when validating the available years and document types.

18.5 Required investigation

The issue should be traced through:

1. Source filing.
2. Ingestion process.
3. Blob structure.
4. Database record.
5. Backend transformation.
6. UI rendering.

The goal is to correct the underlying data rather than simply hide the incorrect values.

19. Foreign-Language Filings

19.1 Problem

Some non-US companies may have filings available in languages other than English.

The ASICS example involved Japanese filing content.

A filing that cannot be read or interpreted by the intended users may not be useful unless translated.

19.2 English-filing preference

The preferred behavior is:

• Search for an English filing when available.
• Prefer the English version during ingestion.
• Avoid loading a foreign-language version if an English version exists.
• Flag records where only a foreign-language filing is available.
• Avoid representing an untranslated filing as fully usable.

19.3 Pipeline transformation concern

The current process may be performing extraction and loading without sufficient transformation.

A proper pipeline should determine:

• What language the filing is in.
• Whether an English version exists.
• Whether translation is required.
• Whether the filing can be normalized.
• Whether the record should be excluded or flagged.

19.4 Business impact

If an untranslated filing is simply downloaded and placed into the system, users may still need to manually translate it.

That defeats the purpose of having the filing available in the product.

20. BOO Data-Quality Issue

20.1 Problem observed

The BOO ticker appeared in the UI, but no filing could be found for it.

This suggests a mismatch between:

• Company records
• Filing records
• UI availability
• Pipeline output

20.2 Possible causes

Potential causes include:

• A company record exists without filing records.
• A stale record remains in the database.
• The company was added incorrectly.
• The ticker-to-company relationship is incorrect.
• Filings were not ingested.
• Filing links are missing.
• The UI does not validate filing availability.
• The database is being populated by a process that is not fully understood.

20.3 Required investigation

The investigation should determine:

1. Why BOO exists in the UI.
2. Which database table contains the company.
3. Whether any filing record exists.
4. Which pipeline inserted the record.
5. Whether the company is expected to have filings.
6. Whether the record should be removed or corrected.
7. Whether similar orphaned records exist.

21. Pipeline Architecture

21.1 Current concern

The discussion suggested that the current pipeline may primarily be performing:

• Extraction
• Loading

with insufficient transformation between those steps.

This may leave the application responsible for:

• Cleaning
• Classification
• Grouping
• Filtering
• Calculations
• Segment identification
• Data normalization

21.2 ETL and ELT concern

The concern is that the pipeline may be closer to an extract-and-load process than a complete extract-transform-load process.

The missing or insufficient transformation stage may be contributing to:

• Incorrect values
• Inconsistent classifications
• Duplicate records
• Slow UI performance
• Repeated processing
• Difficult maintenance
• Inconsistent business logic across pages

22. Bronze, Silver, and Gold Layers

22.1 Bronze layer

The bronze layer should contain raw source data.

Examples include:

• Raw SEC filings
• Raw Yahoo Finance records
• Raw filing metadata
• Raw XBRL facts
• Original filing text
• Original language and document information

The bronze layer should preserve the source data without applying significant business transformations.

22.2 Silver layer

The silver layer should clean and standardize the data.

Examples include:

• Normalized company names
• Standardized tickers
• Normalized executive names
• Cleaned dates
• Standardized titles
• Language classification
• Filing-type normalization
• Duplicate removal
• Data validation
• Source record linkage

22.3 Gold layer

The gold layer should contain product-ready data.

Examples include:

• Calculated tenure
• Total compensation
• Aggregated compensation
• Combined SEC and Yahoo Finance records
• Management-change events
• Geographic classification
• Business-segment classification
• User-facing metrics
• Filter-ready dimensions
• Comparison-ready data

22.4 Desired responsibility

The requested architectural direction is to place transformations in the data layer rather than implementing them separately inside each UI page.

23. Why UI-Side Processing Should Be Minimized

23.1 Performance

If the UI performs extensive processing, users may experience:

• Longer page-load times
• Higher latency
• Slower filter responses
• More memory usage
• Device-dependent behavior
• Network-dependent behavior

23.2 Data consistency

If each page performs its own transformations, different pages may produce different results from the same source data.

Centralized transformations help ensure:

• Consistent calculations
• Consistent classification
• Consistent filtering
• Easier testing
• Easier maintenance
• Easier debugging

23.3 Recommended approach

The preferred architecture is:

1. Extract raw data.
2. Clean and standardize it server-side.
3. Apply business rules in the data layer.
4. Create calculated and aggregated fields.
5. Store product-ready output.
6. Let the UI query and display the results.

24. XBRL Transformation Requirements

The UI or backend may currently receive raw values such as:

• XBRL tag
• Value
• Filing reference

Additional work may be needed to identify:

• Tag type
• Label
• Context
• Member
• Filing section
• Geographic classification
• Operating-segment classification
• Business-segment classification
• Consolidated versus non-consolidated status

This work should be centralized in the data layer wherever possible.

Edgar tools were also discussed as a possible source of cleaner SEC data and may be evaluated for server-side use.

25. Incremental Pipeline Design

The pipeline should support incremental expansion.

For example:

1. Initially, the pipeline may collect 10-K data.
2. A new requirement may add 10-Q data.
3. A later requirement may require specific fields from 10-Q.
4. A subsequent requirement may add 8-K Item 5.02 extraction.
5. Another requirement may add executive-movement calculations.

The pipeline should be designed so that these additions can be made without rebuilding the entire application or duplicating logic in the UI.

25.1 Data-team-driven design

The pipeline should be based on clearly defined requirements:

• What fields are needed?
• Which filings contain them?
• How should they be normalized?
• How should conflicts be resolved?
• Which values should be calculated?
• Which dimensions should be available for filtering?
• Which values should be aggregated?
• Which fields should be exposed to users?

25.2 Product-ready output

The UI should receive data that is already:

• Cleaned
• Standardized
• Validated
• Classified
• Aggregated where necessary
• Ready for filtering
• Ready for comparison

26. Database and Query Behavior

26.1 Database-driven controls

Many UI controls are populated from database tables.

For example, company dropdowns may retrieve names from the Core IQ companies table.

The general flow is:

1. User opens the application.
2. UI requests available company values.
3. Backend opens a database connection.
4. Backend executes a query.
5. Database returns the data.
6. Backend sends the values to the UI.
7. User selects a value.
8. UI sends a new query based on the selection.

26.2 Query structure

The discussion included concepts such as:

• Selecting all columns
• Selecting specific columns
• Table aliases
• Filtering
• Sorting
• Ascending order
• Descending order
• Annual and quarterly period types
• Country filters
• Start dates
• Document types

The interface should translate user selections into efficient backend queries.

26.3 Sorting

Sorting options may include:

• Latest
• Earliest
• Ascending
• Descending

These should be implemented at the query or database level where possible rather than sorting large datasets in the browser.

27. Database Performance

27.1 Connection time

The meeting discussed investigating why database connections and filter loads take time.

Potential areas include:

• Connection setup
• Query complexity
• Missing indexes
• Large result sets
• Repeated requests
• Lack of caching
• Processing in the client
• Unnecessary columns being retrieved

27.2 Indexing

Indexes may improve repeated lookups and filtering.

Possible index candidates include:

• Company identifier
• Ticker
• Executive name
• Filing year
• Filing type
• Item number
• Compensation year
• Country of incorporation
• Industry classification
• Event date

The exact indexes should be based on actual query patterns.

27.3 Caching

Frequently used filter values may be cached so that the system does not repeatedly create a new connection or query the same data.

Caching could improve:

• Dropdown load time
• Repeated searches
• Common company selections
• Frequently used years
• Standard filing filters

28. Currency Conversion

The meeting also discussed currency conversion.

A separate foreign-exchange table may contain:

• Currency pair
• Open rate
• High rate
• Low rate
• Closing rate
• Date

The system may use the relevant closing rate to convert values into the desired currency.

28.1 Recommended processing

Currency conversion should ideally be performed:

• In the data layer
• In a server-side service
• During aggregation
• Before the data reaches the UI

This prevents repeated conversion calculations in the browser.

28.2 Validation

Currency conversion should consider:

• Date of the compensation data
• Date of the exchange rate
• Currency of the source record
• Target currency
• Missing exchange rates
• Historical versus current rates

29. Detailed Responsibilities

The following responsibilities were associated with your work during the meeting.

29.1 People-screening improvements

You should:

1. Review the current people-screening interface.
2. Combine “Other” and “Unknown.”
3. Confirm that compensation-year filtering supports multiple selections.
4. Make the multi-select behavior obvious.
5. Change the presentation so years appear as columns.
6. Investigate why unselected compensation metrics are displayed.
7. Ensure only selected metrics are shown.
8. Check whether company filters are required.
9. Hide data-source and filing-form columns from the normal published view.

29.2 SEC and Yahoo Finance integration

You should:

1. Ask Shashank whether both SEC and Yahoo Finance can be processed for the same US companies.
2. Confirm how the two datasets can be matched.
3. Determine how missing fields should be filled.
4. Investigate memory and storage constraints.
5. Confirm how non-US companies should be handled.
6. Evaluate how duplicate and conflicting records should be resolved.

29.3 Form 8-K Item 5.02 investigation

You should:

1. Review the management-changes spreadsheet.
2. Collect representative Item 5.02 filings.
3. Examine filings covering resignations, appointments, succession, and departures.
4. Identify recurring wording patterns.
5. Test regular-expression approaches.
6. Identify reliable fields.
7. Extract names, titles, dates, event types, and explanatory text.
8. Preserve multiple titles for one executive.
9. Distinguish announcement dates from effective dates.
10. Test whether tenure can be calculated.
11. Report the results to the manager.
12. If reliable, propose implementation.
13. If unreliable, document the limitation and recommend dropping or deferring the idea.

29.4 Data-quality investigation

You should:

1. Investigate the ASICS 7936 future-year issue.
2. Determine why incorrect years exist in the blob.
3. Validate annual versus quarterly records.
4. Check Q4 versus 10-K handling.
5. Investigate untranslated foreign-language filings.
6. Confirm whether English filings can be selected.
7. Investigate why BOO appears in the UI without filings.
8. Trace how these records entered the database.

29.5 Data-layer and performance work

You should:

1. Identify UI-side transformations.
2. Move reusable transformations into the data layer.
3. Establish bronze, silver, and gold processing stages where practical.
4. Create product-ready calculated fields.
5. Evaluate server-side filtering.
6. Evaluate indexing and caching.
7. Preserve UI speed while adding Item 5.02 logic.
8. Determine whether Shashank can include new logic in the recurring pipeline.

30. Detailed Action Plan

Phase 1: Correct current UI behavior

Task 1: Combine executive categories

Change the people filter so that:

• Other
• Unknown

are represented as one category:

• Other

Task 2: Correct metric selection

Confirm that when only salary is selected, the result does not display:

• Bonus
• Stock awards
• Option awards
• Other compensation fields

unless they were selected.

Task 3: Improve multi-year selection

Confirm that users can select multiple compensation years.

Example:

• 2024
• 2025
• 2026

Task 4: Improve display structure

Present selected years as dynamic columns.

Task 5: Hide technical source columns

Remove source and filing-form fields from the default published view.

Phase 2: Integrate SEC and Yahoo Finance

Task 6: Confirm pipeline feasibility

Ask Shashank whether the pipeline can process:

• SEC data
• Yahoo Finance data

for the same US companies.

Task 7: Define matching logic

Determine how to match:

• Company
• Executive
• Title
• Year
• Compensation record

Task 8: Define conflict handling

Determine:

• Which source is preferred for overlapping values
• Whether missing values may be filled from the other source
• How conflicts are flagged
• How duplicate records are prevented

Task 9: Evaluate performance

Measure:

• Pipeline memory
• Storage
• Processing time
• Result size
• UI response time

Phase 3: Prototype Item 5.02 extraction

Task 10: Collect filings

Collect representative examples involving:

• Resignation
• Retirement
• Departure
• Appointment
• Succession
• New executive responsibilities
• Multiple titles

Task 11: Build extraction rules

Test extraction of:

• Names
• Titles
• Dates
• Event types
• Effective dates
• Replacement executives
• Reasons for departure
• Filing metadata

Task 12: Validate results

Compare extracted results with the filing text.

Task 13: Determine feasibility

Decide whether Item 5.02 extraction is:

• Reliable enough for production
• Suitable only for an experimental feature
• Too inconsistent to implement

Phase 4: Add tenure and executive-movement analysis

Only if Phase 3 is reliable:

Task 14: Calculate tenure

Use reliable appointment and departure dates.

Task 15: Calculate role duration

Calculate how long an executive held a specific position.

Task 16: Map executive movement

Match executives moving between companies.

Task 17: Compare compensation

Compare:

• Prior compensation
• New compensation
• Compensation difference
• Prior title
• New title

Phase 5: Correct data quality

Task 18: Fix ASICS

Trace ticker 7936 through:

• Source
• Blob
• Database
• Pipeline
• UI

Task 19: Fix language handling

Prefer English filings or clearly flag untranslated documents.

Task 20: Investigate BOO

Determine why BOO appears without available filings.

Phase 6: Move transformation logic upstream

Task 21: Identify UI processing

List all transformations currently occurring in the UI.

Task 22: Create server-side transformations

Move reusable processing into the data or server layer.

Task 23: Create calculated fields

Precompute:

• Tenure
• Total compensation
• Aggregates
• Segment classifications
• Source-combined records
• Management-change events

Task 24: Optimize queries

Evaluate:

• Indexes
• Caching
• Query reduction
• Selected-column retrieval
• Server-side sorting
• Server-side filtering

31. Risks and Open Questions

31.1 Source matching risk

SEC and Yahoo Finance may use different:

• Names
• Titles
• Years
• Formats
• Definitions

Incorrect matching could produce misleading results.

31.2 Item 5.02 consistency risk

Although Item 5.02 is a promising source, filing language may differ substantially.

The system should not present unvalidated information as definitive.

31.3 Missing dates

Some filings may contain:

• Announcement dates but not effective dates
• Effective dates but not start dates
• Departure dates but not appointment dates
• Approximate timing only

The system should distinguish known from estimated dates.

31.4 Multiple roles

One person may hold multiple executive roles. The data model must support more than one title.

31.5 Foreign-language data

Non-US filings may be difficult to process if no English version exists.

31.6 Memory constraints

Combining multiple data sources and filing types may increase:

• Storage
• Memory
• Processing time
• Database size

31.7 UI speed

Adding more transformation logic directly into the UI could reduce performance. The preferred approach is to process upstream.

31.8 Data ownership and pipeline visibility

The discussion indicated that the database and pipeline are managed separately from the UI. You may need cooperation from Shashank to understand:

• Where data enters the database
• Which pipeline adds it
• How transformations are applied
• How blobs are generated
• How refreshes occur

32. Priority Order

The recommended priority order is:

Priority 1: Fix current people-screening behavior

• Combine Other and Unknown.
• Fix metric selection.
• Add clearer multi-select behavior.
• Show years as columns.
• Hide source and filing-form fields.

Priority 2: Confirm dual-source integration

• SEC plus Yahoo Finance for US companies.
• Yahoo Finance for applicable non-US companies.
• Assess memory and matching requirements.

Priority 3: Investigate Item 5.02

• Test filing patterns.
• Determine extractable fields.
• Decide whether the feature is feasible.

Priority 4: Resolve data-quality issues

• ASICS future years.
• Untranslated filings.
• BOO records without filings.

Priority 5: Move processing into the data layer

• Cleaning
• Standardization
• Classification
• Calculations
• Aggregation
• Query optimization

Priority 6: Build longer-term management intelligence

• Tenure
• Role history
• Succession
• Executive movement
• Compensation changes between companies

33. Final Interpretation of the Manager’s Requests

The concrete direction from the meeting was to improve MIP’s data completeness, usability, and performance.

The most immediate responsibilities are:

1. Make the people-screening interface behave correctly.
2. Support multi-year compensation analysis.
3. Combine SEC and Yahoo Finance information for a fuller executive profile.
4. Hide unnecessary technical source details from the published view.
5. Investigate Form 8-K Item 5.02 as a structured source for management changes.
6. Determine whether tenure and executive-movement calculations are technically reliable.
7. Investigate incorrect filing data and missing filing relationships.
8. Move transformations from the UI to the data layer wherever feasible.
9. Coordinate with Shashank regarding pipeline capability, data quality, and performance.

The manager was open to the Form 8-K management-change concept but did not require it to be implemented without validation. The expected approach is to test the idea, determine what can be extracted reliably, and proceed only if the results are accurate enough to support MIP users.