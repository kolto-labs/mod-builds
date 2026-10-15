import argparse
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 16384
REQUEST_TIMEOUT_SECONDS = 5
IP_SOURCE = 'api4.ipify.org'
GEO_SOURCES = ('ipwho.is', 'ipapi.co', 'ipinfo.io')
STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota',
    'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania',
    'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota',
    'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont',
    'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
}
NAME_TO_STATE = {name.lower(): code for code, name in STATE_NAMES.items()}
SAFE_CITY = re.compile(r"[A-Za-z][A-Za-z .,'()-]{0,99}\Z")


def fetch_json(url):
    try:
        expected = urlsplit(url)
        if (expected.scheme.lower() != 'https' or not expected.hostname
                or expected.username is not None or expected.password is not None):
            return None, {'status': 'invalid_request', 'http_status': None}
    except ValueError:
        return None, {'status': 'invalid_request', 'http_status': None}
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'ModSync-egress-check/1.0'})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            try:
                final = urlsplit(response.geturl())
                final_port = final.port
            except ValueError:
                return None, {'status': 'redirect_rejected', 'http_status': status}
            if (final.scheme.lower() != 'https' or not final.hostname
                    or final.hostname.lower() != expected.hostname.lower()
                    or final_port not in (None, 443)
                    or final.username is not None or final.password is not None):
                return None, {'status': 'redirect_rejected', 'http_status': status}
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        return None, {'status': 'http_error', 'http_status': error.code}
    except (socket.timeout, TimeoutError):
        return None, {'status': 'timeout', 'http_status': None}
    except (URLError, OSError):
        return None, {'status': 'network_error', 'http_status': None}
    if status != 200:
        return None, {'status': 'http_error', 'http_status': status}
    if len(body) > MAX_RESPONSE_BYTES:
        return None, {'status': 'response_too_large', 'http_status': status}
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {'status': 'invalid_response', 'http_status': status}
    if not isinstance(payload, dict):
        return None, {'status': 'invalid_response', 'http_status': status}
    return payload, {'status': 'response_received', 'http_status': status}


def discover_public_ipv4():
    payload, diagnostic = fetch_json('https://api4.ipify.org?format=json')
    diagnostic = {'service': IP_SOURCE, **diagnostic}
    if payload is None:
        return None, diagnostic
    try:
        address = ipaddress.IPv4Address(payload['ip'])
    except (KeyError, ipaddress.AddressValueError, TypeError):
        diagnostic['status'] = 'invalid_response'
        return None, diagnostic
    if not address.is_global:
        diagnostic['status'] = 'non_public_ip'
        return None, diagnostic
    diagnostic['status'] = 'ok'
    return address, diagnostic


def geolocate(service, address):
    path = {
        'ipwho.is': f'https://ipwho.is/{address}',
        'ipapi.co': f'https://ipapi.co/{address}/json/',
        'ipinfo.io': f'https://ipinfo.io/{address}/json',
    }[service]
    payload, diagnostic = fetch_json(path)
    diagnostic = {'service': service, **diagnostic}
    if payload is None:
        return None, diagnostic
    if payload.get('success') is False or payload.get('error'):
        diagnostic['status'] = 'invalid_response'
        return None, diagnostic
    try:
        returned_address = ipaddress.IPv4Address(payload['ip'])
    except (KeyError, ipaddress.AddressValueError, TypeError):
        diagnostic['status'] = 'invalid_response'
        return None, diagnostic
    if returned_address != address:
        diagnostic['status'] = 'ip_mismatch'
        return None, diagnostic
    country_code = payload.get('country_code') or payload.get('country')
    if country_code != 'US':
        diagnostic['status'] = 'non_us'
        return None, diagnostic
    state_code = payload.get('region_code')
    if not isinstance(state_code, str):
        state_name = payload.get('region')
        state_code = NAME_TO_STATE.get(state_name.lower()) if isinstance(state_name, str) else None
    if not isinstance(state_code, str) or state_code not in STATE_NAMES:
        diagnostic['status'] = 'invalid_location'
        return None, diagnostic
    city = payload.get('city')
    if not isinstance(city, str) or not SAFE_CITY.fullmatch(city):
        diagnostic['status'] = 'invalid_location'
        return None, diagnostic
    diagnostic['status'] = 'ok'
    diagnostic['state_code'] = state_code
    return {'state_code': state_code, 'city': city}, diagnostic


def observe():
    address, ip_diagnostic = discover_public_ipv4()
    diagnostics = [ip_diagnostic]
    if address is None:
        return None, diagnostics
    observations = []
    for service in GEO_SOURCES[:2]:
        result, diagnostic = geolocate(service, address)
        diagnostics.append(diagnostic)
        if result is not None:
            observations.append((service, result))

    def state_conflict():
        return len({entry[1]['state_code'] for entry in observations}) > 1

    if state_conflict():
        diagnostics.append({'service': 'corroboration', 'status': 'state_conflict', 'http_status': None})
        return {'ip': str(address)}, diagnostics
    if len(observations) < 2:
        service = GEO_SOURCES[2]
        result, diagnostic = geolocate(service, address)
        diagnostics.append(diagnostic)
        if result is not None:
            observations.append((service, result))
    if state_conflict():
        diagnostics.append({'service': 'corroboration', 'status': 'state_conflict', 'http_status': None})
        return {'ip': str(address)}, diagnostics
    if len(observations) < 2:
        return {'ip': str(address)}, diagnostics
    first, second = observations[:2]
    state_code = first[1]['state_code']
    city_source = next((entry for entry in (first, second) if entry[0] == 'ipwho.is'), first)
    source_urls = {
        'ipwho.is': 'https://ipwho.is/',
        'ipapi.co': 'https://ipapi.co/json/',
        'ipinfo.io': 'https://ipinfo.io/',
    }
    return {
        'ip': str(address),
        'state': STATE_NAMES[state_code],
        'state_code': state_code,
        'city': city_source[1]['city'],
        'country': 'United States',
        'country_code': 'US',
        'provenance': {
            'sources': [source_urls[first[0]], source_urls[second[0]]],
            'ip_source': 'https://api4.ipify.org?format=json',
            'agreement': ['ip', 'state_code', 'country_code'],
            'city_source': source_urls[city_source[0]],
            'description': 'Observed egress location (approximate IP geolocation; not a Steam-guaranteed location).',
        },
    }, diagnostics


def write_result(path, location, diagnostics):
    observed = bool(location and location.get('state_code') in STATE_NAMES)
    result = {
        'status': 'observed' if observed else 'unavailable',
        'ip': location.get('ip') if location else None,
        'state': location.get('state') if location else None,
        'state_code': location.get('state_code') if location else None,
        'city': location.get('city') if location else None,
        'country': location.get('country') if location else None,
        'country_code': location.get('country_code') if location else None,
        'provenance': location.get('provenance') if location and location.get('provenance') else {
            'sources': ['https://ipwho.is/', 'https://ipapi.co/json/', 'https://ipinfo.io/'],
            'ip_source': 'https://api4.ipify.org?format=json',
            'agreement': ['ip', 'state_code', 'country_code'],
            'description': 'Observed egress location (approximate IP geolocation; not a Steam-guaranteed location).',
        },
        'service_results': diagnostics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(result, output, sort_keys=True)
        output.write('\n')
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    location, diagnostics = observe()
    result = write_result(args.output, location, diagnostics)
    if result['status'] == 'observed':
        message = (f"Observed egress: IP {location['ip']}; {location['city']}, "
                   f"{location['state']} ({location['state_code']}), {location['country']}. "
                   'Two independent services agreed for that exact IP; location is approximate, not guaranteed by Steam.')
    else:
        failures = ', '.join(f"{item['service']}={item['status']}" +
                             (f"(HTTP {item['http_status']})" if item.get('http_status') is not None else '')
                             for item in diagnostics)
        ip_note = f" Public IPv4 observed: {result['ip']}." if result['ip'] else ''
        message = ('Public egress IP and US state could not be corroborated; '
                   'Steam authentication is blocked.' + ip_note + ' ' + failures)
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write('Observed egress location is approximate and not Steam-guaranteed.\n\n')
            output.write(message + '\n')
    return 0 if result['status'] == 'observed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
