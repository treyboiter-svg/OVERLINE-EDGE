import requests
import datetime
import pandas as pd
import time
from dateutil import parser
import pytz

OPENWEATHER_API_KEY = '1084e87f28a57d118642798f37fd7b16'
EST = pytz.timezone('America/New_York')
TODAY = datetime.date.today()

LEAGUES = {
    'nfl': 'NFL',
    'mlb': 'MLB',
    'nba': 'NBA',
    'nhl': 'NHL',
    'ncaa_fb': 'NCAA_FB',
    'ncaa_mb': 'NCAA_MB'
}

# ESPN scoreboard endpoints (NCAAM expanded with groups + limit)
ESPN_API_URLS = {
    'nfl':      'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'mlb':      'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard',
    'nba':      'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    'nhl':      'https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard',
    # All FBS CFB (group 80) for a given date
    'ncaa_fb':  f'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&dates={TODAY.strftime("%Y%m%d")}',
    # All Division I MBB (group 50) for a given date; bump limit so you don’t truncate a big slate
    'ncaa_mb':  f'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard'
                f'?groups=50&limit=300&dates={TODAY.strftime("%Y%m%d")}',
}

def convert_to_est(utc_str):
    try:
        dt = parser.isoparse(utc_str)
        return dt.astimezone(EST)
    except Exception as e:
        print(f"Error parsing datetime: {e}")
        return None

def geocode_venue(venue_name, city):
    if not venue_name and not city:
        return None, None
    query = f"{venue_name}, {city}"
    url = "https://nominatim.openstreetmap.org/search"
    params = {'q': query, 'format': 'json', 'limit': 1}
    headers = {'User-Agent': 'GameWeatherApp'}
    try:
        resp = requests.get(url, params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json()
        if results:
            # Be polite to Nominatim
            time.sleep(1)
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as e:
        print(f"Geocoding error for {query}: {e}")
    return None, None

def get_weather(lat, lon):
    if lat is None or lon is None:
        return dict(temp_f=None, pressure_mb=None, wind_mph=None,
                    humidity_pct=None, cloud_cover_pct=None)
    url = 'https://api.openweathermap.org/data/2.5/weather'
    params = {'lat': lat, 'lon': lon,
              'appid': OPENWEATHER_API_KEY, 'units': 'imperial'}
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        main = data.get('main', {})
        wind = data.get('wind', {})
        clouds = data.get('clouds', {})
        return {
            'temp_f': main.get('temp'),
            'pressure_mb': main.get('pressure'),
            'wind_mph': wind.get('speed'),
            'humidity_pct': main.get('humidity'),
            'cloud_cover_pct': clouds.get('all'),
        }
    except Exception as e:
        print(f"OpenWeather fetch error: {e}")
        return dict(temp_f=None, pressure_mb=None, wind_mph=None,
                    humidity_pct=None, cloud_cover_pct=None)

def fetch_espn_games(league):
    url = ESPN_API_URLS[league]
    print(f"Fetching {LEAGUES[league]} games from ESPN...")
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Could not get ESPN data for {LEAGUES[league]}: {e}")
        return []

    games = []
    for event in data.get('events', []):
        comp = event.get('competitions', [{}])[0]
        utc = comp.get('date') or event.get('date')
        dt_est = convert_to_est(utc)
        if not dt_est or dt_est.date() != TODAY:
            continue

        v = comp.get('venue', {})
        loc = v.get('location', {}) or {}
        addr = v.get('address', {}) or {}

        lat = loc.get('latitude')
        lon = loc.get('longitude')
        city = addr.get('city', '')
        name = v.get('fullName') or ''

        if lat is None or lon is None:
            lat, lon = geocode_venue(name, city)

        teams = [c['team']['displayName'] for c in comp.get('competitors', [])]

        games.append({
            'league': LEAGUES[league],
            'kickoff_utc': utc,
            'kickoff_est': dt_est.strftime('%Y-%m-%d %I:%M %p %Z'),
            'venue': name,
            'city': city,
            'latitude': lat,
            'longitude': lon,
            'teams': " vs ".join(teams),
        })

    print(f"Found {len(games)} {LEAGUES[league]} games")
    return games

def fetch_mlb_statsapi_games():
    url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={TODAY.strftime("%Y-%m-%d")}'
    print(f"Fetching MLB games from MLB Stats API for {TODAY}...")
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Error fetching MLB stats API: {e}")
        return []

    games = []
    for day in data.get('dates', []):
        for game in day.get('games', []):
            utc = game.get('gameDate')
            dt_est = convert_to_est(utc)
            home = game['teams']['home']['team']['name']
            away = game['teams']['away']['team']['name']
            venue = game.get('venue', {}).get('name', '')
            city = game.get('venue', {}).get('city', '')
            lat, lon = geocode_venue(venue, city)
            games.append({
                'league': 'MLB',
                'kickoff_utc': utc,
                'kickoff_est': dt_est.strftime('%Y-%m-%d %I:%M %p %Z') if dt_est else '',
                'venue': venue,
                'city': city,
                'latitude': lat,
                'longitude': lon,
                'teams': f"{away} vs {home}",
            })
    print(f"Found {len(games)} MLB games via Stats API")
    return games

def main():
    for key, name in LEAGUES.items():
        games = fetch_espn_games(key)
        if not games and key == 'mlb':
            games = fetch_mlb_statsapi_games()
        if not games:
            print(f"No games for {name}")
            continue

        rows = []
        for g in games:
            w = get_weather(g['latitude'], g['longitude'])
            rows.append({**g, **w})

        df = pd.DataFrame(rows)
        fn = f"{name}_{TODAY}.csv"
        df.to_csv(fn, index=False)
        print(f"Saved {len(rows)} {name} games to {fn}")

if __name__ == "__main__":
    main()