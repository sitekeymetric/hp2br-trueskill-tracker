import discord
from discord.ext import commands
import sqlite3
import os
import asyncio
import trueskill
from typing import List, Optional, Dict, Tuple
import random
import uuid
from datetime import datetime
import re
from itertools import combinations
import math

# Initialize TrueSkill environment
trueskill.setup(mu=25, sigma=8.333, beta=4.166, tau=0.083, draw_probability=0.10)

class TrueSkillDatabase:
    def __init__(self, db_path: str = "trueskill.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize the database with required tables."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                region TEXT DEFAULT 'Unknown',
                mu REAL DEFAULT 25.0,
                sigma REAL DEFAULT 8.333,
                games_played INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS game_players (
                game_id TEXT,
                user_id INTEGER,
                team INTEGER,
                old_mu REAL,
                old_sigma REAL,
                new_mu REAL,
                new_sigma REAL,
                FOREIGN KEY (game_id) REFERENCES games (game_id),
                FOREIGN KEY (user_id) REFERENCES players (user_id)
            )
        ''')
        
        conn.commit()
        
        # Add draws column if it doesn't exist (for existing databases)
        cursor.execute("PRAGMA table_info(players)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'draws' not in columns:
            cursor.execute('ALTER TABLE players ADD COLUMN draws INTEGER DEFAULT 0')
            conn.commit()
        
        conn.close()
    
    def get_player(self, user_id: int) -> Optional[Dict]:
        """Get player data by user ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT user_id, username, region, mu, sigma, games_played, wins, losses, draws
            FROM players WHERE user_id = ?
        ''', (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row[0],
                'username': row[1],
                'region': row[2],
                'mu': row[3],
                'sigma': row[4],
                'games_played': row[5],
                'wins': row[6],
                'losses': row[7],
                'draws': row[8]
            }
        return None
    
    def insert_or_update_player(self, user_id: int, username: str, region: str = None, 
                               mu: float = 25.0, sigma: float = 8.333):
        """Insert new player or update existing player."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        existing_player = self.get_player(user_id)
        
        if existing_player:
            # Update existing player
            cursor.execute('''
                UPDATE players SET username = ?, region = COALESCE(?, region),
                mu = ?, sigma = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (username, region, mu, sigma, user_id))
        else:
            # Insert new player
            cursor.execute('''
                INSERT INTO players (user_id, username, region, mu, sigma)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, username, region or 'Unknown', mu, sigma))
        
        conn.commit()
        conn.close()
    
    def update_player_stats(self, user_id: int, new_rating: trueskill.Rating, 
                           result: str = 'win'):
        """Update player's rating and statistics.
        Args:
            result: 'win', 'loss', or 'draw'
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if result == 'win':
            cursor.execute('''
                UPDATE players SET mu = ?, sigma = ?, games_played = games_played + 1,
                wins = wins + 1, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (new_rating.mu, new_rating.sigma, user_id))
        elif result == 'loss':
            cursor.execute('''
                UPDATE players SET mu = ?, sigma = ?, games_played = games_played + 1,
                losses = losses + 1, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (new_rating.mu, new_rating.sigma, user_id))
        elif result == 'draw':
            cursor.execute('''
                UPDATE players SET mu = ?, sigma = ?, games_played = games_played + 1,
                draws = draws + 1, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (new_rating.mu, new_rating.sigma, user_id))
        
        conn.commit()
        conn.close()
    
    def record_game_result(self, teams: List[List[int]], winning_team_index: int) -> str:
        """Record a game result and update all player ratings."""
        game_id = str(uuid.uuid4())
        
        # Prepare rating groups for TrueSkill calculation
        rating_groups = []
        team_players = []
        
        for team in teams:
            team_ratings = []
            team_player_data = []
            for user_id in team:
                player = self.get_player(user_id)
                if player:
                    rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
                    team_ratings.append(rating)
                    team_player_data.append(player)
                else:
                    # Auto-create player if not exists
                    self.insert_or_update_player(user_id, f"Player_{user_id}")
                    player = self.get_player(user_id)
                    rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
                    team_ratings.append(rating)
                    team_player_data.append(player)
            
            rating_groups.append(team_ratings)
            team_players.append(team_player_data)
        
        # Calculate new ratings
        if winning_team_index == -1:  # Draw
            new_rating_groups = trueskill.rate(rating_groups)
        else:
            # Create ranks (0 for winner, 1 for losers)
            ranks = [1] * len(teams)
            ranks[winning_team_index] = 0
            new_rating_groups = trueskill.rate(rating_groups, ranks=ranks)
        
        # Update database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Record game
        cursor.execute('''
            INSERT INTO games (game_id, completed_at, status)
            VALUES (?, CURRENT_TIMESTAMP, 'completed')
        ''', (game_id,))
        
        # Update player ratings and record game participation
        for team_idx, (team, new_team_ratings) in enumerate(zip(team_players, new_rating_groups)):
            won = team_idx == winning_team_index
            draw = winning_team_index == -1
            
            for player, old_rating, new_rating in zip(team, rating_groups[team_idx], new_team_ratings):
                # Record game participation
                cursor.execute('''
                    INSERT INTO game_players 
                    (game_id, user_id, team, old_mu, old_sigma, new_mu, new_sigma)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (game_id, player['user_id'], team_idx, 
                      old_rating.mu, old_rating.sigma, new_rating.mu, new_rating.sigma))
                
                # Update player stats
                if draw:
                    cursor.execute('''
                        UPDATE players SET mu = ?, sigma = ?, games_played = games_played + 1,
                        draws = draws + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
                    ''', (new_rating.mu, new_rating.sigma, player['user_id']))
                else:
                    result = 'win' if won else 'loss'
                    self.update_player_stats(player['user_id'], new_rating, result)
        
        conn.commit()
        conn.close()
        return game_id
    
    def get_all_players(self) -> List[Dict]:
        """Get all players from database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT user_id, username, region, mu, sigma, games_played, wins, losses, draws
            FROM players ORDER BY mu DESC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [{
            'user_id': row[0],
            'username': row[1],
            'region': row[2],
            'mu': row[3],
            'sigma': row[4],
            'games_played': row[5],
            'wins': row[6],
            'losses': row[7],
            'draws': row[8]
        } for row in rows]

class TeamBalancer:
    @staticmethod
    def get_player_skill(player: Dict) -> float:
        """Get conservative skill estimate for a player."""
        return player['mu'] - 3 * player['sigma']
    
    @staticmethod
    def calculate_team_average(team: List[Dict]) -> float:
        """Calculate average skill of a team."""
        if not team:
            return 0.0
        return sum(TeamBalancer.get_player_skill(p) for p in team) / len(team)
    
    @staticmethod
    def calculate_variance(teams: List[List[Dict]]) -> float:
        """Calculate variance between team averages (lower is better)."""
        if not teams:
            return float('inf')
        
        averages = [TeamBalancer.calculate_team_average(team) for team in teams]
        mean_avg = sum(averages) / len(averages)
        variance = sum((avg - mean_avg) ** 2 for avg in averages) / len(averages)
        return variance
    
    @staticmethod
    def generate_optimal_teams(players: List[Dict], num_teams: int, max_iterations: int = 10000) -> List[List[Dict]]:
        """Generate teams with minimal variance using optimization algorithm."""
        if len(players) < 2 or num_teams < 1:
            return []
        
        # Sort players by skill for better initial distribution
        players_sorted = sorted(players, key=TeamBalancer.get_player_skill, reverse=True)
        
        best_teams = None
        best_variance = float('inf')
        
        # Try multiple random starting configurations
        attempts = min(max_iterations, 1000 if len(players) <= 12 else 500)
        
        for attempt in range(attempts):
            # Create initial teams using round-robin with some randomization
            teams = [[] for _ in range(num_teams)]
            
            # Distribute players in a snake draft pattern with randomization
            player_list = players_sorted.copy()
            if attempt > 0:  # Add randomization after first attempt
                # Shuffle within skill tiers to add variety
                tier_size = max(1, len(player_list) // 4)
                for i in range(0, len(player_list), tier_size):
                    tier = player_list[i:i + tier_size]
                    random.shuffle(tier)
                    player_list[i:i + tier_size] = tier
            
            # Initial distribution using snake draft
            for i, player in enumerate(player_list):
                team_idx = i % num_teams
                if (i // num_teams) % 2 == 1:  # Reverse direction every round
                    team_idx = num_teams - 1 - team_idx
                
                if len(teams[team_idx]) < 4:  # Max 4 players per team
                    teams[team_idx].append(player)
            
            # Local optimization: try swapping players between teams
            improved = True
            iterations = 0
            max_local_iterations = 200 if len(players) <= 8 else 100
            
            while improved and iterations < max_local_iterations:
                improved = False
                iterations += 1
                current_variance = TeamBalancer.calculate_variance(teams)
                
                # Try swapping players between all pairs of teams
                for i in range(num_teams):
                    for j in range(i + 1, num_teams):
                        if not teams[i] or not teams[j]:
                            continue
                        
                        # Try swapping each player from team i with each player from team j
                        for p1_idx, p1 in enumerate(teams[i]):
                            for p2_idx, p2 in enumerate(teams[j]):
                                # Make the swap
                                teams[i][p1_idx], teams[j][p2_idx] = teams[j][p2_idx], teams[i][p1_idx]
                                
                                # Check if this improves balance
                                new_variance = TeamBalancer.calculate_variance(teams)
                                
                                if new_variance < current_variance:
                                    current_variance = new_variance
                                    improved = True
                                else:
                                    # Revert the swap
                                    teams[i][p1_idx], teams[j][p2_idx] = teams[j][p2_idx], teams[i][p1_idx]
            
            # Check if this is the best configuration so far
            final_variance = TeamBalancer.calculate_variance(teams)
            if final_variance < best_variance:
                best_variance = final_variance
                best_teams = [team.copy() for team in teams]
        
        # Remove empty teams
        if best_teams:
            return [team for team in best_teams if team]
        
        return []
    
    @staticmethod
    def balance_teams(players: List[Dict], num_teams: int = 2) -> List[List[Dict]]:
        """Balance players into teams using optimized algorithm for minimal variance.
        
        Args:
            players: List of player dictionaries
            num_teams: Desired number of teams (will be capped at 5)
        
        Returns:
            List of teams with minimized average skill variance
        """
        if len(players) < 2:
            return []
        
        # Cap number of teams at 5
        max_teams = min(5, num_teams)
        
        # Calculate optimal number of teams based on player count
        total_players = len(players)
        
        if total_players <= 4:
            optimal_teams = 2 if total_players >= 2 else 1
        elif total_players <= 8:
            optimal_teams = 2
        elif total_players <= 12:
            optimal_teams = 3
        elif total_players <= 16:
            optimal_teams = 4
        else:
            optimal_teams = 5
        
        # Use the smaller of requested teams or optimal teams
        final_teams = min(max_teams, optimal_teams)
        
        # Ensure we can make at least 2 players per team (when possible)
        while final_teams > 1 and (total_players // final_teams) < 2:
            final_teams -= 1
        
        if final_teams < 1:
            return []
        
        # Use the optimization algorithm
        return TeamBalancer.generate_optimal_teams(players, final_teams)
    
    @staticmethod
    def balance_teams_with_region(players: List[Dict], required_region: str) -> List[List[Dict]]:
        """Balance players into teams ensuring each team has at least one player from the required region.
        
        Args:
            players: List of player dictionaries
            required_region: Region that must be represented in every team
        
        Returns:
            List of teams, each containing at least one player from required_region
        """
        if len(players) < 2:
            return []
        
        # Separate players by region
        region_players = [p for p in players if p.get('region', '').lower() == required_region.lower()]
        other_players = [p for p in players if p.get('region', '').lower() != required_region.lower()]
        
        # Check if we have enough region players to form teams
        if not region_players:
            return []  # Cannot form teams if no players from required region
        
        # Maximum teams is limited by number of region players (since each team needs one)
        max_possible_teams = min(len(region_players), 5)  # Cap at 5 teams max
        
        # Calculate optimal team count based on total players
        total_players = len(players)
        if total_players <= 4:
            optimal_teams = min(2, max_possible_teams) if total_players >= 2 else 1
        elif total_players <= 8:
            optimal_teams = min(2, max_possible_teams)
        elif total_players <= 12:
            optimal_teams = min(3, max_possible_teams)
        elif total_players <= 16:
            optimal_teams = min(4, max_possible_teams)
        else:
            optimal_teams = min(5, max_possible_teams)
        
        # Ensure we don't create more teams than we have region players for
        final_teams = min(optimal_teams, len(region_players))
        
        # If we can't make at least 2 players per team, reduce team count
        while final_teams > 1 and (total_players // final_teams) < 2:
            final_teams -= 1
        
        if final_teams < 1:
            return []
        
        # Use optimized algorithm with region constraint
        return TeamBalancer.generate_optimal_teams_with_region(
            region_players, other_players, final_teams
        )
    
    @staticmethod
    def generate_optimal_teams_with_region(
        region_players: List[Dict], 
        other_players: List[Dict], 
        num_teams: int
    ) -> List[List[Dict]]:
        """Generate region-constrained teams with minimal variance."""
        if num_teams < 1 or not region_players:
            return []
        
        # Sort both groups by skill
        region_sorted = sorted(region_players, key=TeamBalancer.get_player_skill, reverse=True)
        other_sorted = sorted(other_players, key=TeamBalancer.get_player_skill, reverse=True)
        
        best_teams = None
        best_variance = float('inf')
        
        # Try multiple configurations
        attempts = min(500, 100 if len(region_players + other_players) > 12 else 200)
        
        for attempt in range(attempts):
            teams = [[] for _ in range(num_teams)]
            
            # First, assign one region player to each team (required constraint)
            region_list = region_sorted.copy()
            if attempt > 0:
                # Add some randomization for variety
                tier_size = max(1, len(region_list) // 3)
                for i in range(0, len(region_list), tier_size):
                    tier = region_list[i:i + tier_size]
                    random.shuffle(tier)
                    region_list[i:i + tier_size] = tier
            
            # Assign one region player to each team
            for i in range(min(num_teams, len(region_list))):
                teams[i].append(region_list[i])
            
            # Collect remaining players (both region and other)
            remaining_region = region_list[num_teams:]
            all_remaining = remaining_region + other_sorted
            
            if attempt > 0 and all_remaining:
                # Shuffle within skill tiers for variety
                tier_size = max(1, len(all_remaining) // 4)
                for i in range(0, len(all_remaining), tier_size):
                    tier = all_remaining[i:i + tier_size]
                    random.shuffle(tier)
                    all_remaining[i:i + tier_size] = tier
            
            # Distribute remaining players using snake draft
            for i, player in enumerate(all_remaining):
                team_idx = i % num_teams
                if (i // num_teams) % 2 == 1:  # Snake pattern
                    team_idx = num_teams - 1 - team_idx
                
                if len(teams[team_idx]) < 4:  # Max 4 players per team
                    teams[team_idx].append(player)
            
            # Local optimization with swapping
            improved = True
            iterations = 0
            max_local_iterations = 150 if len(all_remaining) <= 8 else 75
            
            while improved and iterations < max_local_iterations:
                improved = False
                iterations += 1
                current_variance = TeamBalancer.calculate_variance(teams)
                
                # Try swapping non-constraint players between teams
                for i in range(num_teams):
                    for j in range(i + 1, num_teams):
                        if len(teams[i]) <= 1 or len(teams[j]) <= 1:
                            continue  # Skip if team only has the required region player
                        
                        # Try swapping players (excluding the first one which is the region constraint)
                        for p1_idx in range(1, len(teams[i])):
                            for p2_idx in range(1, len(teams[j])):
                                # Make the swap
                                teams[i][p1_idx], teams[j][p2_idx] = teams[j][p2_idx], teams[i][p1_idx]
                                
                                # Check if this improves balance
                                new_variance = TeamBalancer.calculate_variance(teams)
                                
                                if new_variance < current_variance:
                                    current_variance = new_variance
                                    improved = True
                                else:
                                    # Revert the swap
                                    teams[i][p1_idx], teams[j][p2_idx] = teams[j][p2_idx], teams[i][p1_idx]
            
            # Check if this is the best configuration
            final_variance = TeamBalancer.calculate_variance(teams)
            if final_variance < best_variance:
                best_variance = final_variance
                best_teams = [team.copy() for team in teams]
        
        # Remove empty teams
        if best_teams:
            return [team for team in best_teams if team]
        
        return []

class PersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

class TeamCreationView(PersistentView):
    def __init__(self, bot, teams: List[List[Dict]], waiting_room_id: int):
        super().__init__()
        self.bot = bot
        self.teams = teams
        self.waiting_room_id = waiting_room_id
        self.temp_channels = []
    
    @discord.ui.button(label="Create Teams & Move Players", style=discord.ButtonStyle.green, emoji="🚀", custom_id="create_teams_move")
    async def create_and_move(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Create temporary channels and move players to their teams."""
        try:
            guild = interaction.guild
            waiting_room = guild.get_channel(self.waiting_room_id)
            
            if not waiting_room:
                await interaction.response.send_message("❌ Waiting Room channel not found!", ephemeral=True)
                return
            
            # Create temporary voice channels for each team
            category = waiting_room.category
            temp_channels = []
            
            for i, team in enumerate(self.teams, 1):
                channel_name = f"Team {i} - Game {uuid.uuid4().hex[:6]}"
                temp_channel = await guild.create_voice_channel(
                    name=channel_name,
                    category=category
                )
                temp_channels.append(temp_channel)
                self.temp_channels.append(temp_channel)
                
                # Move team members to their channel
                for player in team:
                    member = guild.get_member(player['user_id'])
                    if member and member.voice and member.voice.channel == waiting_room:
                        try:
                            await member.move_to(temp_channel)
                        except discord.HTTPException:
                            pass  # Member might have left or moved
            
            # Update the view to show End Game button
            end_game_view = EndGameView(self.bot, self.temp_channels, self.waiting_room_id)
            
            embed = discord.Embed(
                title="🎮 Teams Created!",
                description="Players have been moved to their team channels.",
                color=discord.Color.green()
            )
            
            await interaction.response.edit_message(embed=embed, view=end_game_view)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error creating teams: {str(e)}", ephemeral=True)

class EndGameView(PersistentView):
    def __init__(self, bot, temp_channels: List[discord.VoiceChannel], waiting_room_id: int):
        super().__init__()
        self.bot = bot
        self.temp_channels = temp_channels
        self.waiting_room_id = waiting_room_id
    
    @discord.ui.button(label="End Game", style=discord.ButtonStyle.red, emoji="🏁", custom_id="end_game_button")
    async def end_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Move all players back to waiting room and delete temporary channels."""
        try:
            guild = interaction.guild
            waiting_room = guild.get_channel(self.waiting_room_id)
            
            if not waiting_room:
                await interaction.response.send_message("❌ Waiting Room channel not found!", ephemeral=True)
                return
            
            # Move all players back to waiting room
            for channel in self.temp_channels:
                if channel:
                    for member in channel.members:
                        try:
                            await member.move_to(waiting_room)
                        except discord.HTTPException:
                            pass
            
            # Delete temporary channels
            for channel in self.temp_channels:
                if channel:
                    try:
                        await channel.delete()
                    except discord.HTTPException:
                        pass
            
            embed = discord.Embed(
                title="🏁 Game Ended",
                description="All players moved back to Waiting Room and temporary channels deleted.",
                color=discord.Color.blue()
            )
            
            # Remove all buttons by sending a view with no items
            empty_view = discord.ui.View()
            await interaction.response.edit_message(embed=embed, view=empty_view)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error ending game: {str(e)}", ephemeral=True)

class TrueSkillBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        super().__init__(command_prefix='!', intents=intents)
        self.db = TrueSkillDatabase()
        self.current_teams = {}  # Store current teams by guild_id
    
    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print(f'Bot is in {len(self.guilds)} guilds')
        
        # Don't add views here - they'll be created dynamically when needed

bot = TrueSkillBot()

@bot.group(name='ts', invoke_without_command=True)
async def trueskill_command(ctx):
    """Main TrueSkill command group."""
    embed = discord.Embed(
        title="🏆 TrueSkill Bot Commands",
        description="Available commands:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Player Management",
        value="`!ts update <@user> [region]` - Update player info\n"
              "`!ts insert <@user> [region]` - Add new player\n"
              "`!ts view [@user]` - View player stats\n"
              "`!ts leaderboard` - Show top players",
        inline=False
    )
    embed.add_field(
        name="Team Management",
        value="`!ts teams` - Create balanced teams from Waiting Room\n"
              "`!ts teams <region>` - Create teams with at least one player from region\n"
              "`!ts cleanup` - Move all players back to Waiting Room and delete temp channels",
        inline=False
    )
    embed.add_field(
        name="Game Results",
        value="`!ts win <@user>` - Record a win for user\n"
              "`!ts loss <@user>` - Record a loss for user\n"
              "`!ts draw <@user>` - Record a draw for user\n"
              "`!ts teamwin <team_number>` - Award team members a win\n"
              "`!ts teamloss <team_number>` - Award team members a loss\n"
              "`!ts teamdraw <team_number>` - Award team members a draw\n"
              "`!ts draw <team1_players...> vs <team2_players...>` - Record draw\n"
              "`!ts 1v1 <@winner> <@loser>` - Record 1v1 match",
        inline=False
    )
    
    await ctx.send(embed=embed)

@trueskill_command.command(name='update')
async def update_player(ctx, member: discord.Member, region: str = None):
    """Update player information."""
    try:
        existing_player = bot.db.get_player(member.id)
        if existing_player:
            bot.db.insert_or_update_player(
                member.id, 
                member.display_name, 
                region,
                existing_player['mu'],
                existing_player['sigma']
            )
            embed = discord.Embed(
                title="✅ Player Updated",
                description=f"Updated {member.display_name}'s information.",
                color=discord.Color.green()
            )
            if region:
                embed.add_field(name="Region", value=region, inline=True)
        else:
            embed = discord.Embed(
                title="❌ Player Not Found",
                description=f"{member.display_name} is not in the database. Use `!ts insert` first.",
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error updating player: {str(e)}")

@trueskill_command.command(name='insert')
async def insert_player(ctx, member: discord.Member, region: str = "Unknown"):
    """Insert a new player into the database."""
    try:
        existing_player = bot.db.get_player(member.id)
        if existing_player:
            embed = discord.Embed(
                title="⚠️ Player Already Exists",
                description=f"{member.display_name} is already in the database. Use `!ts update` to modify.",
                color=discord.Color.orange()
            )
        else:
            bot.db.insert_or_update_player(member.id, member.display_name, region)
            embed = discord.Embed(
                title="✅ Player Added",
                description=f"Added {member.display_name} to the database.",
                color=discord.Color.green()
            )
            embed.add_field(name="Region", value=region, inline=True)
            embed.add_field(name="Initial Rating", value="25.0 ± 8.33", inline=True)
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error inserting player: {str(e)}")

@trueskill_command.command(name='view')
async def view_player(ctx, member: discord.Member = None):
    """View player statistics."""
    if member is None:
        member = ctx.author
    
    try:
        player = bot.db.get_player(member.id)
        if player:
            rating = player['mu'] - 3 * player['sigma']  # Conservative skill estimate
            winrate = (player['wins'] / player['games_played'] * 100) if player['games_played'] > 0 else 0
            
            embed = discord.Embed(
                title=f"📊 {player['username']}'s Stats",
                color=discord.Color.blue()
            )
            embed.add_field(name="Region", value=player['region'], inline=True)
            embed.add_field(name="Rating (μ)", value=f"{player['mu']:.2f}", inline=True)
            embed.add_field(name="Uncertainty (σ)", value=f"{player['sigma']:.2f}", inline=True)
            embed.add_field(name="Conservative Skill", value=f"{rating:.2f}", inline=True)
            embed.add_field(name="Games Played", value=player['games_played'], inline=True)
            embed.add_field(name="Win Rate", value=f"{winrate:.1f}%", inline=True)
            embed.add_field(name="Wins", value=player['wins'], inline=True)
            embed.add_field(name="Losses", value=player['losses'], inline=True)
            embed.add_field(name="Draws", value=player['draws'], inline=True)
        else:
            embed = discord.Embed(
                title="❌ Player Not Found",
                description=f"{member.display_name} is not in the database.",
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error viewing player: {str(e)}")

@trueskill_command.command(name='leaderboard')
async def leaderboard(ctx, limit: int = 20):
    """Show the leaderboard (default: top 20 players)."""
    try:
        players = bot.db.get_all_players()
        if not players:
            embed = discord.Embed(
                title="📋 Leaderboard",
                description="No players found in the database.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return
        
        # Sort by conservative skill estimate
        players.sort(key=lambda p: p['mu'] - 3 * p['sigma'], reverse=True)
        
        embed = discord.Embed(
            title="🏆 TrueSkill Leaderboard",
            color=discord.Color.gold()
        )
        
        leaderboard_text = ""
        for i, player in enumerate(players[:limit], 1):
            conservative_skill = player['mu'] - 3 * player['sigma']
            winrate = (player['wins'] / player['games_played'] * 100) if player['games_played'] > 0 else 0
            
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            leaderboard_text += f"{medal} **{player['username']}** - {conservative_skill:.1f} ({player['games_played']} games, {winrate:.1f}% WR)\n"
        
        embed.description = leaderboard_text
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error generating leaderboard: {str(e)}")

@trueskill_command.command(name='teams')
async def create_teams(ctx, region: str = None):
    """Create balanced teams from players in the Waiting Room.
    
    Args:
        region: Optional region - if provided, ensures each team has at least one player from this region
    """
    try:
        # Find the "Waiting Room" voice channel
        waiting_room = None
        for channel in ctx.guild.voice_channels:
            if channel.name.lower() == "waiting room":
                waiting_room = channel
                break
        
        if not waiting_room:
            embed = discord.Embed(
                title="❌ Waiting Room Not Found",
                description="Please create a voice channel named 'Waiting Room'.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # Get members in the waiting room
        members_in_room = waiting_room.members
        if len(members_in_room) < 2:
            embed = discord.Embed(
                title="❌ Not Enough Players",
                description="Need at least 2 players in the Waiting Room to create teams.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # Check if we have too many players
        if len(members_in_room) > 20:
            embed = discord.Embed(
                title="⚠️ Too Many Players",
                description=f"Found {len(members_in_room)} players. Maximum supported is 20 players (5 teams × 4 players each).\n"
                          f"Only the first 20 players will be used for team balancing.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            members_in_room = members_in_room[:20]  # Limit to first 20 players
        
        # Get player data for members in the waiting room
        players = []
        for member in members_in_room:
            player_data = bot.db.get_player(member.id)
            if not player_data:
                # Auto-register new players
                bot.db.insert_or_update_player(member.id, member.display_name)
                player_data = bot.db.get_player(member.id)
            players.append(player_data)
        
        # Balance teams based on whether region is specified
        if region:
            teams = TeamBalancer.balance_teams_with_region(players, region)
            
            # Check if region-based balancing failed
            if not teams:
                # Count players from the specified region
                region_count = sum(1 for p in players if p.get('region', '').lower() == region.lower())
                
                embed = discord.Embed(
                    title="❌ Cannot Create Region-Based Teams",
                    description=f"Cannot create teams with region requirement '{region}'.\n"
                              f"Found {region_count} players from '{region}' region.\n"
                              f"Need at least 1 player from '{region}' to create teams.",
                    color=discord.Color.red()
                )
                
                # Show available regions
                available_regions = list(set(p.get('region', 'Unknown') for p in players if p.get('region')))
                if available_regions:
                    embed.add_field(
                        name="Available Regions",
                        value=", ".join(available_regions),
                        inline=False
                    )
                
                await ctx.send(embed=embed)
                return
        else:
            # Standard team balancing
            teams = TeamBalancer.balance_teams(players)
        
        if not teams:
            embed = discord.Embed(
                title="❌ Cannot Create Teams",
                description="Unable to balance players into teams.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # Create embed showing team composition
        if region:
            embed = discord.Embed(
                title="⚖️ Region-Based Balanced Teams",
                description=f"Created {len(teams)} balanced teams from {len(players)} players:\n"
                           f"🌍 Each team has at least one '{region}' player\n"
                           f"📊 Max 4 players per team | Max 5 teams total",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="⚖️ Optimally Balanced Teams",
                description=f"Created {len(teams)} optimally balanced teams from {len(players)} players:\n"
                           f"📊 Max 4 players per team | Max 5 teams total",
                color=discord.Color.green()
            )
        
        for i, team in enumerate(teams, 1):
            team_strength = sum(TeamBalancer.get_player_skill(p) for p in team) / len(team)
            team_text = ""
            
            for player in team:
                conservative_skill = TeamBalancer.get_player_skill(player)
                if region:
                    # Show region info when using region-based balancing
                    player_region = player.get('region', 'Unknown')
                    region_indicator = " 🌍" if player_region.lower() == region.lower() else ""
                    team_text += f"• {player['username']} ({conservative_skill:.1f}) [{player_region}]{region_indicator}\n"
                else:
                    team_text += f"• {player['username']} ({conservative_skill:.1f})\n"
            
            embed.add_field(
                name=f"Team {i} (Avg: {team_strength:.1f})",
                value=team_text,
                inline=True
            )
        
        # Add balance statistics
        team_averages = [TeamBalancer.calculate_team_average(team) for team in teams]
        if len(team_averages) > 1:
            min_avg = min(team_averages)
            max_avg = max(team_averages)
            variance = TeamBalancer.calculate_variance(teams)
            balance_quality = "Excellent" if variance < 0.5 else "Good" if variance < 2.0 else "Fair"
            
            embed.add_field(
                name="🎯 Balance Quality",
                value=f"{balance_quality}\n"
                     f"Range: {min_avg:.1f} - {max_avg:.1f}\n"
                     f"Difference: {max_avg - min_avg:.1f}",
                inline=True
            )
        
        # Store teams for win/loss tracking
        bot.current_teams[ctx.guild.id] = teams
        
        # Create view with persistent buttons
        view = TeamCreationView(bot, teams, waiting_room.id)
        await ctx.send(embed=embed, view=view)
        
    except Exception as e:
        await ctx.send(f"❌ Error creating teams: {str(e)}")

@trueskill_command.command(name='cleanup')
async def cleanup_teams(ctx):
    """Move all players back to Waiting Room and delete temporary channels."""
    try:
        # Find the "Waiting Room" voice channel
        waiting_room = None
        for channel in ctx.guild.voice_channels:
            if channel.name.lower() == "waiting room":
                waiting_room = channel
                break
        
        if not waiting_room:
            embed = discord.Embed(
                title="❌ Waiting Room Not Found",
                description="Please create a voice channel named 'Waiting Room'.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # Find all temporary team channels (channels that start with "Team " and contain " - Game ")
        temp_channels = []
        players_moved = 0
        channels_deleted = 0
        
        for channel in ctx.guild.voice_channels:
            # Check if this looks like a temporary team channel
            if (channel.name.startswith("Team ") and " - Game " in channel.name) or \
               ("team" in channel.name.lower() and channel != waiting_room and 
                channel.category == waiting_room.category):
                temp_channels.append(channel)
        
        # Move all players from temporary channels back to waiting room
        for channel in temp_channels:
            for member in list(channel.members):  # Use list() to avoid modification during iteration
                try:
                    await member.move_to(waiting_room)
                    players_moved += 1
                except discord.HTTPException:
                    pass  # Member might have left or moved already
        
        # Delete temporary channels
        for channel in temp_channels:
            try:
                await channel.delete()
                channels_deleted += 1
            except discord.HTTPException:
                pass  # Channel might already be deleted or no permission
        
        # Clear stored teams for this guild
        if ctx.guild.id in bot.current_teams:
            del bot.current_teams[ctx.guild.id]
        
        # Create success embed
        embed = discord.Embed(
            title="🧹 Cleanup Complete",
            description="Successfully cleaned up team channels and moved players back.",
            color=discord.Color.green()
        )
        embed.add_field(name="Players Moved", value=str(players_moved), inline=True)
        embed.add_field(name="Channels Deleted", value=str(channels_deleted), inline=True)
        embed.add_field(name="Destination", value=waiting_room.mention, inline=True)
        
        if players_moved == 0 and channels_deleted == 0:
            embed = discord.Embed(
                title="ℹ️ No Cleanup Needed",
                description="No temporary team channels or players found to clean up.",
                color=discord.Color.blue()
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error during cleanup: {str(e)}")

@trueskill_command.command(name='win')
async def record_win(ctx, member: discord.Member):
    """Record a win for a specific user."""
    try:
        # Check if player exists in database
        player = bot.db.get_player(member.id)
        if not player:
            # Auto-register new player
            bot.db.insert_or_update_player(member.id, member.display_name)
            player = bot.db.get_player(member.id)
        
        # Create a basic rating increase for a win
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        # Simulate a win against an average opponent
        new_rating = trueskill.rate_1vs1(old_rating, trueskill.Rating())[0]
        
        # Update player stats
        bot.db.update_player_stats(member.id, new_rating, 'win')
        
        embed = discord.Embed(
            title="✅ Win Recorded",
            description=f"Recorded a win for {member.display_name}",
            color=discord.Color.green()
        )
        embed.add_field(name="Old Rating", value=f"{old_rating.mu:.2f} ± {old_rating.sigma:.2f}", inline=True)
        embed.add_field(name="New Rating", value=f"{new_rating.mu:.2f} ± {new_rating.sigma:.2f}", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording win: {str(e)}")

@trueskill_command.command(name='loss')
async def record_loss(ctx, member: discord.Member):
    """Record a loss for a specific user."""
    try:
        # Check if player exists in database
        player = bot.db.get_player(member.id)
        if not player:
            # Auto-register new player
            bot.db.insert_or_update_player(member.id, member.display_name)
            player = bot.db.get_player(member.id)
        
        # Create a basic rating decrease for a loss
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        # Simulate a loss against an average opponent
        new_rating = trueskill.rate_1vs1(old_rating, trueskill.Rating())[1]
        
        # Update player stats
        bot.db.update_player_stats(member.id, new_rating, 'loss')
        
        embed = discord.Embed(
            title="❌ Loss Recorded",
            description=f"Recorded a loss for {member.display_name}",
            color=discord.Color.red()
        )
        embed.add_field(name="Old Rating", value=f"{old_rating.mu:.2f} ± {old_rating.sigma:.2f}", inline=True)
        embed.add_field(name="New Rating", value=f"{new_rating.mu:.2f} ± {new_rating.sigma:.2f}", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording loss: {str(e)}")

@trueskill_command.command(name='draw')
async def record_draw(ctx, member: discord.Member):
    """Record a draw for a specific user."""
    try:
        # Check if player exists in database
        player = bot.db.get_player(member.id)
        if not player:
            # Auto-register new player
            bot.db.insert_or_update_player(member.id, member.display_name)
            player = bot.db.get_player(member.id)
        
        # For a draw, rating stays mostly the same but uncertainty might change slightly
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        # Simulate a draw - rating should stay close to the same
        # We'll use a small adjustment by rating against an equal opponent as a draw
        rating_groups = [[old_rating], [trueskill.Rating(mu=player['mu'], sigma=player['sigma'])]]
        new_rating_groups = trueskill.rate(rating_groups, ranks=[0, 0])  # Both get rank 0 (tie)
        new_rating = new_rating_groups[0][0]
        
        # Update player stats
        bot.db.update_player_stats(member.id, new_rating, 'draw')
        
        embed = discord.Embed(
            title="🤝 Draw Recorded",
            description=f"Recorded a draw for {member.display_name}",
            color=discord.Color.orange()
        )
        embed.add_field(name="Old Rating", value=f"{old_rating.mu:.2f} ± {old_rating.sigma:.2f}", inline=True)
        embed.add_field(name="New Rating", value=f"{new_rating.mu:.2f} ± {new_rating.sigma:.2f}", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording draw: {str(e)}")

@trueskill_command.command(name='teamwin')
async def record_team_win(ctx, team_number: int):
    """Record a win for all members of a specific team."""
    try:
        # Check if there are current teams
        if ctx.guild.id not in bot.current_teams or not bot.current_teams[ctx.guild.id]:
            embed = discord.Embed(
                title="❌ No Active Teams",
                description="No teams found. Use `!ts teams` to create teams first.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        teams = bot.current_teams[ctx.guild.id]
        
        # Validate team number
        if team_number < 1 or team_number > len(teams):
            embed = discord.Embed(
                title="❌ Invalid Team Number",
                description=f"Team number must be between 1 and {len(teams)}.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        winning_team = teams[team_number - 1]
        wins_recorded = []
        
        # Record wins for all team members
        for player_data in winning_team:
            old_rating = trueskill.Rating(mu=player_data['mu'], sigma=player_data['sigma'])
            # Simulate a win against an average opponent
            new_rating = trueskill.rate_1vs1(old_rating, trueskill.Rating())[0]
            
            # Update player stats
            bot.db.update_player_stats(player_data['user_id'], new_rating, 'win')
            wins_recorded.append({
                'name': player_data['username'],
                'old_rating': old_rating,
                'new_rating': new_rating
            })
        
        embed = discord.Embed(
            title=f"🏆 Team {team_number} Win Recorded",
            description=f"Recorded wins for all {len(winning_team)} members of Team {team_number}",
            color=discord.Color.green()
        )
        
        for win in wins_recorded:
            embed.add_field(
                name=win['name'],
                value=f"{win['old_rating'].mu:.1f} → {win['new_rating'].mu:.1f}",
                inline=True
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording team win: {str(e)}")

@trueskill_command.command(name='teamloss')
async def record_team_loss(ctx, team_number: int):
    """Record a loss for all members of a specific team."""
    try:
        # Check if there are current teams
        if ctx.guild.id not in bot.current_teams or not bot.current_teams[ctx.guild.id]:
            embed = discord.Embed(
                title="❌ No Active Teams",
                description="No teams found. Use `!ts teams` to create teams first.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        teams = bot.current_teams[ctx.guild.id]
        
        # Validate team number
        if team_number < 1 or team_number > len(teams):
            embed = discord.Embed(
                title="❌ Invalid Team Number",
                description=f"Team number must be between 1 and {len(teams)}.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        losing_team = teams[team_number - 1]
        losses_recorded = []
        
        # Record losses for all team members
        for player_data in losing_team:
            old_rating = trueskill.Rating(mu=player_data['mu'], sigma=player_data['sigma'])
            # Simulate a loss against an average opponent
            new_rating = trueskill.rate_1vs1(old_rating, trueskill.Rating())[1]
            
            # Update player stats
            bot.db.update_player_stats(player_data['user_id'], new_rating, 'loss')
            losses_recorded.append({
                'name': player_data['username'],
                'old_rating': old_rating,
                'new_rating': new_rating
            })
        
        embed = discord.Embed(
            title=f"💔 Team {team_number} Loss Recorded",
            description=f"Recorded losses for all {len(losing_team)} members of Team {team_number}",
            color=discord.Color.red()
        )
        
        for loss in losses_recorded:
            embed.add_field(
                name=loss['name'],
                value=f"{loss['old_rating'].mu:.1f} → {loss['new_rating'].mu:.1f}",
                inline=True
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording team loss: {str(e)}")

@trueskill_command.command(name='teamdraw')
async def record_team_draw(ctx, team_number: int):
    """Record a draw for all members of a specific team."""
    try:
        # Check if there are current teams
        if ctx.guild.id not in bot.current_teams or not bot.current_teams[ctx.guild.id]:
            embed = discord.Embed(
                title="❌ No Active Teams",
                description="No teams found. Use `!ts teams` to create teams first.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        teams = bot.current_teams[ctx.guild.id]
        
        # Validate team number
        if team_number < 1 or team_number > len(teams):
            embed = discord.Embed(
                title="❌ Invalid Team Number",
                description=f"Team number must be between 1 and {len(teams)}.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        drawing_team = teams[team_number - 1]
        draws_recorded = []
        
        # Record draws for all team members
        for player_data in drawing_team:
            old_rating = trueskill.Rating(mu=player_data['mu'], sigma=player_data['sigma'])
            # Simulate a draw against an equal opponent
            rating_groups = [[old_rating], [trueskill.Rating(mu=player_data['mu'], sigma=player_data['sigma'])]]
            new_rating_groups = trueskill.rate(rating_groups, ranks=[0, 0])  # Both get rank 0 (tie)
            new_rating = new_rating_groups[0][0]
            
            # Update player stats
            bot.db.update_player_stats(player_data['user_id'], new_rating, 'draw')
            draws_recorded.append({
                'name': player_data['username'],
                'old_rating': old_rating,
                'new_rating': new_rating
            })
        
        embed = discord.Embed(
            title=f"🤝 Team {team_number} Draw Recorded",
            description=f"Recorded draws for all {len(drawing_team)} members of Team {team_number}",
            color=discord.Color.orange()
        )
        
        for draw in draws_recorded:
            embed.add_field(
                name=draw['name'],
                value=f"{draw['old_rating'].mu:.1f} → {draw['new_rating'].mu:.1f}",
                inline=True
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error recording team draw: {str(e)}")

# Error handlers
@update_player.error
@insert_player.error
@view_player.error
@record_win.error
@record_loss.error
@record_draw.error
async def player_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Please mention a valid user.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing required argument. Please check the command usage.")

@record_team_win.error
@record_team_loss.error
@record_team_draw.error
async def team_command_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid team number. Please provide a valid number.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing team number. Usage: `!ts teamwin <team_number>`, `!ts teamloss <team_number>`, or `!ts teamdraw <team_number>`")

@cleanup_teams.error
async def cleanup_command_error(ctx, error):
    await ctx.send(f"❌ Error during cleanup: {str(error)}")

# Run the bot
if __name__ == "__main__":
    token = os.environ.get('TRUESKILL_TOKEN')
    if not token:
        print("❌ Error: TRUESKILL_TOKEN environment variable not found!")
        print("Please set your Discord bot token in the TRUESKILL_TOKEN environment variable.")
        exit(1)
    
    try:
        bot.run(token)
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
