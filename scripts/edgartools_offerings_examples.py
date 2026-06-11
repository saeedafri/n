#!/usr/bin/env python3
"""
Working examples for extracting securities offering information using edgartools.

This script demonstrates how to:
1. Find IPO filings (S-1, 424B4)
2. Track secondary offerings (424B2, 424B5)
3. Extract deal terms from Form C (crowdfunding)
4. Extract deal terms from Form D (private placements)
5. Monitor shelf registrations and takedowns
6. Classify 424B filings by offering type
"""

from edgar import Company, get_filings, Filing
from edgar.offerings import FormC, FormD, Offering, group_offerings_by_file_number
from edgar.current_filings import get_current_filings
from edgar.shelfofferings import list_takedown_forms, takedown_forms
from edgar.enums import FormType
from datetime import date
import re


def example_1_find_ipo_filings():
    """Find IPO filings for a company."""
    print("=" * 80)
    print("EXAMPLE 1: Finding IPO Filings")
    print("=" * 80)
    
    # Method 1: Find S-1 filings for a specific company
    company = Company("AAPL")
    s1_filings = company.get_filings(form=["S-1", "S-1/A"])
    
    print(f"\nFound {len(s1_filings)} S-1 filings for {company.name}")
    
    for filing in s1_filings[:3]:
        print(f"\n  Form: {filing.form}")
        print(f"  Date: {filing.filing_date}")
        print(f"  Accession: {filing.accession_no}")
        print(f"  File Number: {filing.file_number}")
        
        # For S-1, extract text/markdown (no structured obj available)
        # markdown = filing.markdown()  # Uncomment to download
    
    # Method 2: Find 424B4 filings (final IPO prospectuses)
    filings_424b4 = company.get_filings(form="424B4")
    print(f"\nFound {len(filings_424b4)} 424B4 filings for {company.name}")


def example_2_track_secondary_offerings():
    """Track secondary offerings using 424B forms."""
    print("\n" + "=" * 80)
    print("EXAMPLE 2: Tracking Secondary Offerings")
    print("=" * 80)
    
    company = Company("TSLA")
    
    # 424B2 = Prospectus for already registered securities (common for secondaries)
    # 424B5 = Prospectus for additional securities (ATM offerings)
    # 424B3 = Prospectus with material changes
    secondary_filings = company.get_filings(form=["424B2", "424B5", "424B3"])
    
    print(f"\nFound {len(secondary_filings)} potential secondary offerings for {company.name}")
    
    for filing in secondary_filings[:5]:
        print(f"\n  {filing.form} filed on {filing.filing_date}")
        
        # Quick content analysis (without downloading full text)
        # For production, you'd use filing.text() or filing.markdown()
        
        # Check filing size as proxy for complexity
        print(f"  Accession: {filing.accession_no}")


def example_3_extract_form_c_crowdfunding():
    """Extract detailed information from Form C crowdfunding filings."""
    print("\n" + "=" * 80)
    print("EXAMPLE 3: Extracting Form C Crowdfunding Data")
    print("=" * 80)
    
    # Using a known crowdfunding issuer (ViiT Health - CIK 1881570)
    # In practice, you'd search for companies with Form C filings
    
    try:
        company = Company("1881570")
        form_c_filings = company.get_filings(form=["C", "C/A", "C-U"])
        
        print(f"\nFound {len(form_c_filings)} Form C filings for {company.name}")
        
        for filing in form_c_filings[:2]:
            # Parse the Form C structured data
            formc: FormC = filing.obj()
            
            print(f"\n  Form: {formc.description}")
            print(f"  Issuer: {formc.issuer_name}")
            print(f"  CIK: {formc.issuer_cik}")
            
            # Offering information
            if formc.offering_information:
                offer = formc.offering_information
                print(f"\n  Offering Terms:")
                print(f"    Security Type: {offer.security_description}")
                
                if offer.target_amount:
                    print(f"    Target Amount: ${offer.target_amount:,.2f}")
                if offer.maximum_offering_amount:
                    print(f"    Maximum Amount: ${offer.maximum_offering_amount:,.2f}")
                if offer.price_per_security:
                    print(f"    Price per Unit: ${offer.price_per_security:,.2f}")
                if offer.number_of_securities:
                    print(f"    Number of Units: {offer.number_of_securities:,}")
                if offer.deadline_date:
                    print(f"    Deadline: {offer.deadline_date}")
                    print(f"    Days Remaining: {formc.days_to_deadline}")
            
            # Funding portal info
            if formc.issuer_information.funding_portal:
                portal = formc.issuer_information.funding_portal
                print(f"\n  Funding Portal: {portal.name}")
                print(f"    Portal CIK: {portal.cik}")
                print(f"    Portal File Number: {portal.file_number}")
            
            # Campaign status
            print(f"\n  Campaign Status: {formc.campaign_status}")
            print(f"  Is Expired: {formc.is_expired}")
    
    except Exception as e:
        print(f"\n  Note: Example company may not have recent filings: {e}")


def example_4_extract_form_d_private_placement():
    """Extract detailed information from Form D private placement filings."""
    print("\n" + "=" * 80)
    print("EXAMPLE 4: Extracting Form D Private Placement Data")
    print("=" * 80)
    
    # Search for recent Form D filings
    try:
        recent_form_d = get_current_filings(form="D", page_size=20)
        
        print(f"\nFound {len(recent_form_d)} recent Form D filings")
        
        for filing in list(recent_form_d)[:3]:
            print(f"\n  Filing: {filing.accession_number}")
            print(f"  Company: {filing.company}")
            print(f"  CIK: {filing.cik}")
            print(f"  Date: {filing.filing_date}")
            
            # Get full filing details
            full_filing = Filing(
                cik=filing.cik,
                company=filing.company,
                form=filing.form,
                filing_date=str(filing.filing_date),
                accession_no=filing.accession_number
            )
            
            # Parse structured data
            try:
                formd: FormD = full_filing.obj()
                
                if formd and formd.offering_data:
                    data = formd.offering_data
                    
                    # Industry and size
                    print(f"  Industry: {data.industry_group.industry_group_type}")
                    print(f"  Revenue Range: {data.revenue_range}")
                    
                    # Exemptions
                    if data.federal_exemptions:
                        print(f"  Exemptions: {', '.join(data.federal_exemptions)}")
                    
                    # Amounts
                    if data.offering_sales_amounts:
                        amounts = data.offering_sales_amounts
                        print(f"  Total Offering: {amounts.total_offering_amount}")
                        print(f"  Amount Sold: {amounts.total_amount_sold}")
                    
                    # Minimum investment
                    if data.minimum_investment:
                        print(f"  Minimum Investment: {data.minimum_investment}")
                    
                    # Investors
                    if data.investors:
                        print(f"  Total Investors: {data.investors.total_already_invested}")
                        print(f"  Non-Accredited: {data.investors.has_non_accredited_investors}")
                    
                    # Sales compensation (placement agents)
                    if data.sales_compensation_recipients:
                        print(f"  Placement Agents: {len(data.sales_compensation_recipients)}")
                        for recipient in data.sales_compensation_recipients[:2]:
                            print(f"    - {recipient.name} ({recipient.associated_bd_name})")
            
            except Exception as e:
                print(f"  Could not parse Form D details: {e}")
    
    except Exception as e:
        print(f"\n  Error accessing current filings: {e}")


def example_5_offering_lifecycle_tracking():
    """Track the complete lifecycle of a crowdfunding offering."""
    print("\n" + "=" * 80)
    print("EXAMPLE 5: Offering Lifecycle Tracking")
    print("=" * 80)
    
    try:
        company = Company("1881570")
        
        # Get all Form C variants
        all_form_c = company.get_filings(form=['C', 'C/A', 'C-U', 'C-AR', 'C-TR'])
        
        print(f"\nFound {len(all_form_c)} total Form C filings")
        
        # Group by file number
        grouped = group_offerings_by_file_number(all_form_c)
        
        print(f"Grouped into {len(grouped)} distinct offerings\n")
        
        for file_number, filings in list(grouped.items())[:2]:
            print(f"\n  Offering File Number: {file_number}")
            print(f"  Total filings in lifecycle: {len(filings)}")
            
            # Create Offering object
            offering = Offering(file_number, cik=str(company.cik))
            
            print(f"  Status: {offering.current_status}")
            print(f"  Launch Date: {offering.launch_date}")
            print(f"  Days Active: {offering.days_since_launch}")
            print(f"  Is Active: {offering.is_active}")
            print(f"  Is Terminated: {offering.is_terminated}")
            
            # Show timeline
            print(f"\n  Timeline:")
            for event in offering.timeline():
                print(f"    {event['date']}: {event['form']} - {event['description']}")
            
            # Latest financials
            fin = offering.latest_financials()
            if fin:
                print(f"\n  Latest Financials:")
                print(f"    Assets: ${fin.total_assets:,.2f}")
                print(f"    Cash: ${fin.cash_and_cash_equivalents:,.2f}")
                print(f"    Revenue: ${fin.revenues:,.2f}")
                print(f"    Net Income: ${fin.net_income:,.2f}")
                print(f"    Employees: {fin.current_employees}")
    
    except Exception as e:
        print(f"\n  Note: Example company may not have recent filings: {e}")


def example_6_shelf_registration_tracking():
    """Monitor shelf registrations and takedowns."""
    print("\n" + "=" * 80)
    print("EXAMPLE 6: Shelf Registration and Takedown Monitoring")
    print("=" * 80)
    
    # List all takedown form types
    print("\nShelf Takedown Form Types:")
    for form in takedown_forms:
        print(f"  - {form}")
    
    # Get descriptions
    print("\nTakedown Form Descriptions:")
    try:
        takedown_df = list_takedown_forms()
        print(takedown_df.head(10).to_string())
    except Exception as e:
        print(f"  Could not retrieve descriptions: {e}")
    
    # Track shelf for a company
    company = Company("AAPL")
    
    # Find shelf registrations
    shelf_regs = company.get_filings(form=["S-3", "S-3/A"])
    print(f"\nFound {len(shelf_regs)} S-3 shelf registrations for {company.name}")
    
    for reg in shelf_regs[:2]:
        print(f"\n  Shelf Registration: {reg.accession_no}")
        print(f"  Filed: {reg.filing_date}")
        print(f"  File Number: {reg.file_number}")
        
        # Get related filings by file number
        if reg.file_number:
            related = company.get_filings(file_number=reg.file_number)
            takedowns = [f for f in related if f.form.startswith("424B")]
            
            print(f"  Total related filings: {len(related)}")
            print(f"  Takedowns (424B forms): {len(takedowns)}")
            
            for takedown in takedowns[:5]:
                print(f"    - {takedown.form} on {takedown.filing_date}")


def example_7_extract_424b_deal_terms():
    """Extract deal terms from 424B filings using text parsing."""
    print("\n" + "=" * 80)
    print("EXAMPLE 7: Extracting Deal Terms from 424B Filings")
    print("=" * 80)
    
    def extract_offering_terms(filing):
        """Extract offering terms from 424B filing."""
        terms = {
            'form': filing.form,
            'date': filing.filing_date,
            'company': filing.company,
            'offering_type': None,
            'amount': None,
        }
        
        # Get text content
        try:
            text = filing.text()[:10000].lower()  # First 10K chars for speed
            
            # Detect offering type
            if "initial public offering" in text or "ipo" in text:
                terms['offering_type'] = "IPO"
            elif "at the market" in text or "sales agreement" in text:
                terms['offering_type'] = "ATM"
            elif "follow-on" in text or "secondary" in text:
                terms['offering_type'] = "Secondary"
            elif "debt" in text or "notes" in text:
                terms['offering_type'] = "Debt"
            
            # Try to extract amount
            amount_pattern = r'\$([\d,]+(?:\.\d{2})?)\s*million'
            match = re.search(amount_pattern, text)
            if match:
                amount = float(match.group(1).replace(',', ''))
                terms['amount'] = amount * 1000000
        
        except Exception as e:
            terms['error'] = str(e)
        
        return terms
    
    # Find recent 424B filings
    company = Company("TSLA")
    filings_424b = company.get_filings(form=["424B4", "424B5", "424B2"])
    
    print(f"\nFound {len(filings_424b)} 424B filings for {company.name}")
    print("\nAnalyzing first 3 filings...")
    
    for filing in filings_424b[:3]:
        terms = extract_offering_terms(filing)
        
        print(f"\n  Form: {terms['form']}")
        print(f"  Date: {terms['date']}")
        print(f"  Offering Type: {terms['offering_type'] or 'Unknown'}")
        if terms['amount']:
            print(f"  Amount: ${terms['amount']:,.0f}")
        else:
            print(f"  Amount: Not extracted")


def example_8_classify_424b_offerings():
    """Classify 424B filings by offering type."""
    print("\n" + "=" * 80)
    print("EXAMPLE 8: Classifying 424B Offerings by Type")
    print("=" * 80)
    
    def classify_offering(filing):
        """Classify 424B filing by offering type."""
        try:
            text = filing.text()[:5000].lower()
            
            indicators = {
                'PIPE': ['private investment', 'pipe', 'private placement', 'subscription agreement'],
                'ATM': ['at the market', 'sales agreement', 'equity distribution'],
                'Follow-On': ['follow-on', 'public offering', 'underwriting agreement'],
                'Debt': ['senior notes', 'convertible notes', 'debt securities', 'indenture'],
                'IPO': ['initial public offering', 'ipo'],
            }
            
            scores = {}
            for offering_type, keywords in indicators.items():
                score = sum(1 for keyword in keywords if keyword in text)
                if score > 0:
                    scores[offering_type] = score
            
            if scores:
                return max(scores, key=scores.get)
            return "Other"
        
        except Exception:
            return "Error"
    
    # Get 424B filings for analysis
    company = Company("AAPL")
    filings = company.get_filings(form=["424B2", "424B4", "424B5"])
    
    print(f"\nAnalyzing {len(filings)} 424B filings for {company.name}")
    
    classifications = {}
    for filing in filings:
        offering_type = classify_offering(filing)
        classifications.setdefault(offering_type, []).append({
            'form': filing.form,
            'date': filing.filing_date,
        })
    
    print("\nClassification Results:")
    for offering_type, items in sorted(classifications.items(), key=lambda x: -len(x[1])):
        print(f"\n  {offering_type}: {len(items)} filings")
        for item in items[:3]:
            print(f"    - {item['form']} ({item['date']})")


def example_9_use_enums_for_type_safety():
    """Demonstrate using FormType enums for type safety."""
    print("\n" + "=" * 80)
    print("EXAMPLE 9: Using FormType Enums")
    print("=" * 80)
    
    # Using enums instead of raw strings
    from edgar.enums import FormType, REGISTRATION_FORMS, PROXY_FORMS
    
    print("\nRegistration Forms:")
    for form in REGISTRATION_FORMS:
        print(f"  - {form.value}")
    
    print("\nProxy Forms:")
    for form in PROXY_FORMS:
        print(f"  - {form.value}")
    
    # Using specific prospectus forms
    prospectus_forms = [
        FormType.PROSPECTUS_424B1,
        FormType.PROSPECTUS_424B2,
        FormType.PROSPECTUS_424B3,
        FormType.PROSPECTUS_424B4,
        FormType.PROSPECTUS_424B5,
    ]
    
    print("\nProspectus Forms:")
    for form in prospectus_forms:
        print(f"  - {form.value}")
    
    # Using in queries
    company = Company("AAPL")
    
    # Type-safe form query
    filings = company.get_filings(form=FormType.REGISTRATION_S3)
    print(f"\nS-3 filings found: {len(filings)}")


def example_10_monitor_current_offerings():
    """Monitor today's offerings in real-time."""
    print("\n" + "=" * 80)
    print("EXAMPLE 10: Monitoring Current Offerings")
    print("=" * 80)
    
    print("\nFetching today's Form D filings...")
    
    try:
        current = get_current_filings(form="D", page_size=40)
        
        print(f"\nFound {len(current)} Form D filings today")
        
        for filing in list(current)[:5]:
            print(f"\n  Company: {filing.company}")
            print(f"  CIK: {filing.cik}")
            print(f"  Form: {filing.form}")
            print(f"  Accepted: {filing.accepted}")
            print(f"  Accession: {filing.accession_number}")
    
    except Exception as e:
        print(f"  Error: {e}")
    
    print("\nFetching today's 424B filings...")
    
    try:
        current_424b = get_current_filings(form="424B4", page_size=40)
        
        print(f"\nFound {len(current_424b)} 424B4 filings today")
        
        for filing in list(current_424b)[:5]:
            print(f"\n  Company: {filing.company}")
            print(f"  CIK: {filing.cik}")
            print(f"  Accepted: {filing.accepted}")
    
    except Exception as e:
        print(f"  Error: {e}")


if __name__ == "__main__":
    """Run all examples."""
    
    print("\n" + "=" * 80)
    print("EDGARTOOLS SECURITIES OFFERINGS EXTRACTION EXAMPLES")
    print("=" * 80)
    
    # Run examples
    example_1_find_ipo_filings()
    example_2_track_secondary_offerings()
    example_3_extract_form_c_crowdfunding()
    example_4_extract_form_d_private_placement()
    example_5_offering_lifecycle_tracking()
    example_6_shelf_registration_tracking()
    example_7_extract_424b_deal_terms()
    example_8_classify_424b_offerings()
    example_9_use_enums_for_type_safety()
    example_10_monitor_current_offerings()
    
    print("\n" + "=" * 80)
    print("ALL EXAMPLES COMPLETED")
    print("=" * 80)
    print("\nFor detailed documentation, see:")
    print("  - /Users/mohdsaeedafri/Library/Python/3.14/lib/python/site-packages/edgar/offerings/")
    print("  - docs/edgartools-offerings-analysis.md")
