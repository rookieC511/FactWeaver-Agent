import os
from tools import scrape_jina_ai

def analyze_large_url(url: str, output_file: str):
    print(f"Fetching {url}...")
    content = scrape_jina_ai(url)
    
    total_chars = len(content)
    print(f"Total Characters Extracted: {total_chars}")
    
    if total_chars == 0:
        print("Failed to fetch or empty content.")
        return
        
    line_count = content.count('\n')
    print(f"Total Lines: {line_count}")
    
    # Save a report
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"URL: {url}\n")
        f.write(f"Total Chars: {total_chars}\n")
        f.write(f"Total Lines: {line_count}\n")
        f.write("="*50 + "\n")
        
        f.write("--- FIRST 5000 CHARS ---\n")
        f.write(content[:5000])
        f.write("\n\n")
        
        if total_chars > 20000:
            f.write("--- MIDDLE 5000 CHARS ---\n")
            mid_start = total_chars // 2 - 2500
            f.write(content[mid_start:mid_start+5000])
            f.write("\n\n")
            
        f.write("--- LAST 5000 CHARS ---\n")
        f.write(content[-5000:])
        f.write("\n")
        
    print(f"Analysis saved to {output_file}")

if __name__ == "__main__":
    # Test with a massive Wikipedia page
    test_url = "https://en.wikipedia.org/wiki/Economy_of_China"
    analyze_large_url(test_url, "debug_scraper_output.txt")
