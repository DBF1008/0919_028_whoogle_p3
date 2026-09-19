"""Google Custom Search Engine (CSE) API Client

This module provides a client for Google's Custom Search JSON API,
allowing users to bring their own API key (BYOK) for search functionality.
"""

import httpx
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlparse

from flask import render_template


# Google Custom Search API endpoint
CSE_API_URL = 'https://www.googleapis.com/customsearch/v1'


class CSEException(Exception):
    """Exception raised for CSE API errors"""
    def __init__(self, message: str, code: int = 500, is_quota_error: bool = False):
        self.message = message
        self.code = code
        self.is_quota_error = is_quota_error
        super().__init__(self.message)


@dataclass
class CSEError:
    """Represents an error from the CSE API"""
    code: int
    message: str
    
    @property
    def is_quota_exceeded(self) -> bool:
        return self.code == 429 or 'quota' in self.message.lower()
    
    @property
    def is_invalid_key(self) -> bool:
        return self.code == 400 or 'invalid' in self.message.lower()


@dataclass
class CSEResult:
    """Represents a single search result from CSE API"""
    title: str
    link: str
    snippet: str
    display_link: str
    html_title: Optional[str] = None
    html_snippet: Optional[str] = None
    # Image-specific fields (populated for image search)
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    context_link: Optional[str] = None  # Page where image was found


@dataclass
class CSEResponse:
    """Represents a complete CSE API response"""
    results: list[CSEResult]
    total_results: str
    search_time: float
    query: str
    start_index: int
    is_image_search: bool = False
    error: Optional[CSEError] = None
    
    @property
    def has_error(self) -> bool:
        return self.error is not None
    
    @property
    def has_results(self) -> bool:
        return len(self.results) > 0


class CSEClient:
    """Client for Google Custom Search Engine API
    
    Usage:
        client = CSEClient(api_key='your-key', cse_id='your-cse-id')
        response = client.search('python programming')
        
        if response.has_error:
            print(f"Error: {response.error.message}")
        else:
            for result in response.results:
                print(f"{result.title}: {result.link}")
    """
    
    def __init__(self, api_key: str, cse_id: str, timeout: float = 10.0):
        """Initialize CSE client
        
        Args:
            api_key: Google API key with Custom Search API enabled
            cse_id: Custom Search Engine ID (cx parameter)
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.cse_id = cse_id
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
    
    def search(
        self,
        query: str,
        start: int = 1,
        num: int = 10,
        safe: str = 'off',
        language: str = '',
        country: str = '',
        search_type: str = ''
    ) -> CSEResponse:
        """Execute a search query against the CSE API
        
        Args:
            query: Search query string
            start: Starting result index (1-based, for pagination)
            num: Number of results to return (max 10)
            safe: Safe search setting ('off', 'medium', 'high')
            language: Language restriction (e.g., 'lang_en')
            country: Country restriction (e.g., 'countryUS')
            search_type: Type of search ('image' for image search, '' for web)
            
        Returns:
            CSEResponse with results or error information
        """
        params = {
            'key': self.api_key,
            'cx': self.cse_id,
            'q': query,
            'start': start,
            'num': min(num, 10),  # API max is 10
            'safe': safe,
        }
        
        # Add search type for image search
        if search_type == 'image':
            params['searchType'] = 'image'
        
        # Add optional parameters
        if language:
            # CSE uses 'lr' for language restrict
            params['lr'] = language
        if country:
            # CSE uses 'cr' for country restrict
            params['cr'] = country
        
        try:
            response = self._client.get(CSE_API_URL, params=params)
            data = response.json()
            
            # Check for API errors
            if 'error' in data:
                error_info = data['error']
                return CSEResponse(
                    results=[],
                    total_results='0',
                    search_time=0.0,
                    query=query,
                    start_index=start,
                    error=CSEError(
                        code=error_info.get('code', 500),
                        message=error_info.get('message', 'Unknown error')
                    )
                )
            
            # Parse successful response
            search_info = data.get('searchInformation', {})
            items = data.get('items', [])
            is_image = search_type == 'image'
            
            results = []
            for item in items:
                # Extract image-specific data if present
                image_data = item.get('image', {})
                
                results.append(CSEResult(
                    title=item.get('title', ''),
                    link=item.get('link', ''),
                    snippet=item.get('snippet', ''),
                    display_link=item.get('displayLink', ''),
                    html_title=item.get('htmlTitle'),
                    html_snippet=item.get('htmlSnippet'),
                    # Image fields
                    image_url=item.get('link') if is_image else None,
                    thumbnail_url=image_data.get('thumbnailLink'),
                    image_width=image_data.get('width'),
                    image_height=image_data.get('height'),
                    context_link=image_data.get('contextLink')
                ))
            
            return CSEResponse(
                results=results,
                total_results=search_info.get('totalResults', '0'),
                search_time=float(search_info.get('searchTime', 0)),
                query=query,
                start_index=start,
                is_image_search=is_image
            )
            
        except httpx.TimeoutException:
            return CSEResponse(
                results=[],
                total_results='0',
                search_time=0.0,
                query=query,
                start_index=start,
                error=CSEError(code=408, message='Request timed out')
            )
        except httpx.RequestError as e:
            return CSEResponse(
                results=[],
                total_results='0',
                search_time=0.0,
                query=query,
                start_index=start,
                error=CSEError(code=500, message=f'Request failed: {str(e)}')
            )
        except Exception as e:
            return CSEResponse(
                results=[],
                total_results='0',
                search_time=0.0,
                query=query,
                start_index=start,
                error=CSEError(code=500, message=f'Unexpected error: {str(e)}')
            )
    
    def close(self):
        """Close the HTTP client"""
        self._client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


def cse_results_to_html(response: CSEResponse, query: str) -> str:
    """Convert CSE API response to HTML matching Whoogle's result format
    
    This generates HTML that mimics the structure expected by Whoogle's
    existing filter and result processing pipeline.
    
    Args:
        response: CSEResponse from the API
        query: Original search query
        
    Returns:
        HTML string formatted like Google search results
    """
    if response.has_error:
        error = response.error
        if error.is_quota_exceeded:
            return _error_html(
                'API Quota Exceeded',
                'Your Google Custom Search API quota has been exceeded. '
                'Free tier allows 100 queries/day. Wait until midnight PT '
                'or enable billing in Google Cloud Console.'
            )
        elif error.is_invalid_key:
            return _error_html(
                'Invalid API Key',
                'Your Google Custom Search API key is invalid. '
                'Please check your API key and CSE ID in settings.'
            )
        else:
            return _error_html('Search Error', error.message)
    
    if not response.has_results:
        return _no_results_html(query)
    
    # Use different HTML structure for image vs web results
    if response.is_image_search:
        return _image_results_html(response, query)
    
    # Build HTML results matching Whoogle's expected structure
    results_html = []
    
    for result in response.results:
        # Escape HTML in content
        title = _escape_html(result.title)
        snippet = _escape_html(result.snippet)
        link = result.link
        display_link = _escape_html(result.display_link)
        
        # Use HTML versions if available (they have bold tags for query terms)
        if result.html_title:
            title = result.html_title
        if result.html_snippet:
            snippet = result.html_snippet
        
        # Match the structure used by Google/mock results
        result_html = f'''
        <div class="ZINbbc xpd O9g5cc uUPGi">
            <div class="kCrYT">
                <a href="{link}">
                    <h3 class="BNeawe vvjwJb AP7Wnd">{title}</h3>
                    <div class="BNeawe UPmit AP7Wnd luh4tb" style="color: var(--whoogle-result-url);">{display_link}</div>
                </a>
            </div>
            <div class="kCrYT">
                <div class="BNeawe s3v9rd AP7Wnd">
                    <span class="VwiC3b">{snippet}</span>
                </div>
            </div>
        </div>
        '''
        results_html.append(result_html)
    
    # Build pagination if needed
    pagination_html = ''
    if int(response.total_results) > 10:
        pagination_html = _pagination_html(response.start_index, response.query)
    
    # Wrap in expected structure
    # Add data-cse attribute to prevent collapse_sections from collapsing these results
    return f'''
    <html>
    <body>
        <div id="main" data-cse="true">
            <div id="cnt">
                <div id="rcnt">
                    <div id="center_col">
                        <div id="res">
                            <div id="search">
                                <div id="rso">
                                    {''.join(results_html)}
                                </div>
                            </div>
                        </div>
                        {pagination_html}
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    '''


def _escape_html(text: str) -> str:
    """Escape HTML special characters"""
    if not text:
        return ''
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _error_html(title: str, message: str) -> str:
    """Generate error HTML"""
    return f'''
    <html>
    <body>
        <div id="main">
            <div style="padding: 20px; text-align: center;">
                <h2 style="color: #d93025;">{_escape_html(title)}</h2>
                <p>{_escape_html(message)}</p>
            </div>
        </div>
    </body>
    </html>
    '''


def _no_results_html(query: str) -> str:
    """Generate no results HTML"""
    return f'''
    <html>
    <body>
        <div id="main">
            <div style="padding: 20px;">
                <p>No results found for <b>{_escape_html(query)}</b></p>
            </div>
        </div>
    </body>
    </html>
    '''


def _image_results_html(response: CSEResponse, query: str) -> str:
    """Generate HTML for image search results using the imageresults template
    
    Args:
        response: CSEResponse with image results
        query: Original search query
        
    Returns:
        HTML string formatted for image results display
    """
    # Convert CSE results to the format expected by imageresults.html template
    results = []
    for result in response.results:
        image_url = result.image_url or result.link
        thumbnail_url = result.thumbnail_url or image_url
        web_page = result.context_link or result.link
        domain = urlparse(web_page).netloc if web_page else result.display_link
        
        results.append({
            'domain': domain,
            'img_url': image_url,
            'web_page': web_page,
            'img_tbn': thumbnail_url
        })
    
    # Build pagination link if needed
    next_link = None
    if int(response.total_results) > response.start_index + len(response.results) - 1:
        next_start = response.start_index + 10
        next_link = f'search?q={query}&tbm=isch&start={next_start}'
    
    # Use the same template as regular image results
    return render_template(
        'imageresults.html',
        length=len(results),
        results=results,
        view_label="View Image",
        next_link=next_link
    )


def _pagination_html(current_start: int, query: str) -> str:
    """Generate pagination links"""
    # CSE API uses 1-based indexing, 10 results per page
    current_page = (current_start - 1) // 10 + 1
    
    prev_link = ''
    next_link = ''
    
    if current_page > 1:
        prev_start = (current_page - 2) * 10 + 1
        prev_link = f'<a href="search?q={query}&start={prev_start}">Previous</a>'
    
    next_start = current_page * 10 + 1
    next_link = f'<a href="search?q={query}&start={next_start}">Next</a>'
    
    return f'''
    <div id="foot" style="text-align: center; padding: 20px;">
        {prev_link}
        <span style="margin: 0 20px;">Page {current_page}</span>
        {next_link}
    </div>
    '''
