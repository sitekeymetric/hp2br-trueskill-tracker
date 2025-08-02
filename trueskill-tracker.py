import discord
from discord.ext import commands
import os
import asyncio
import trueskill
from typing import List, Optional, Dict, Tuple, Any
import random
import uuid
import aiohttp
from itertools import combinations
import math

# Initialize TrueSkill environment
trueskill.setup(mu=25, sigma=8.333, beta=4.166, tau=0.083, draw_probability=0.10)

# API Configuration
TRUESKILL_API_URL = os.environ.get('TRUESKILL_API_URL', 'http://127.0.0.1:8081')

class TrueSkillAPIClient:
    """A client to interact with the TrueSkill Database Microservice."""
    def __init__(self, base_url: str):
        self.base_url = base_url

    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Helper method for making API requests."""
        async with aiohttp.ClientSession() as session:
            try:
                url = f"{self.base_url}{endpoint}"
                async with session.request(method, url, **kwargs) as response:
                    response.raise_for_status()
                    return await response.json()
            except aiohttp.ClientError as e:
                print(f"API Error for {method} {endpoint}: {e}")
                raise commands.CommandError(f"Error communicating with the TrueSkill microservice: {e}")
    
    async def get_player(self, user_id: int) -> Optional[Dict]:
        """Fetches a single player's data by user ID."""
        try:
            return await self._request('GET', f"/players/{user_id}")
        except commands.CommandError:
            return None

    async def get_all_players(self) -> List[Dict]:
        """Fetches all players from the database."""
        return await self._request('GET', "/players")
    
    async def insert_or_update_player(self, user_id: int, username: str, region: Optional[str] = "Unknown"):
        """Inserts a new player or updates an existing one."""
        payload = {
            "user_id": user_id,
            "username": username,
            "region": region
        }
        await self._request('POST', "/players", json=payload)

    async def update_player_stats(self, user_id: int, mu: float, sigma: float, result: str):
        """Updates a player's rating and game statistics."""
        payload = {
            "mu": mu,
            "sigma": sigma,
            "result": result
        }
        await self._request('PUT', f"/players/{user_id}/stats", json=payload)
    
    async def record_game(self, teams: List[List[int]]) -> str:
        """Records a new game and returns the game_id."""
        payload = {
            "teams": teams,
            "winning_team_index": -1  # Placeholder, not used by microservice directly
        }
        response = await self._request('POST', "/games/record", json=payload)
        return response['game_id']

    async def update_game_player_rating(self, game_id: str, user_id: int, new_mu: float, new_sigma: float):
        """Updates the final rating for a player within a recorded game."""
        return await self._request('PUT', f"/games/{game_id}/players/{user_id}/rating?new_mu={new_mu}&new_sigma={new_sigma}")

class TeamBalancer:
    """Handles the logic for balancing teams based on TrueSkill ratings."""
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
    def generate_optimal_teams(players: List[Dict], num_teams: int, max_iterations: int = 10000, use_randomization: bool = False) -> List[List[Dict]]:
        """Generate teams with minimal variance using optimization algorithm.
        
        Args:
            use_randomization: If True, applies more aggressive randomization
        """
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
            if attempt > 0 or use_randomization:  # Add randomization after first attempt or if requested
                if use_randomization:
                    # More aggressive randomization - shuffle entire skill tiers
                    tier_size = max(2, len(player_list) // 3)  # Larger tiers for more randomness
                    for i in range(0, len(player_list), tier_size):
                        tier = player_list[i:i + tier_size]
                        random.shuffle(tier)
                        player_list[i:i + tier_size] = tier
                else:
                    # Standard randomization - shuffle within smaller skill tiers
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
    def balance_teams(players: List[Dict], num_teams: int = 2, use_randomization: bool = False) -> List[List[Dict]]:
        """Balance players into teams using optimized algorithm for minimal variance.
        
        Args:
            players: List of player dictionaries
            num_teams: Desired number of teams (will be capped at 5)
            use_randomization: If True, adds more randomization to team generation
        
        Returns:
            List of teams with minimized average skill variance
        """
        if len(players) < 2:
            return []
        
        total_players = len(players)
        
        # Calculate optimal number of teams based on player count
        # Aim for 2-4 players per team, prefer more balanced distribution
        if total_players <= 4:
            optimal_teams = 2 if total_players >= 4 else 1
        elif total_players <= 6:
            optimal_teams = 3  # 2 players per team
        elif total_players <= 8:
            optimal_teams = 2  # 4 players per team
        elif total_players <= 12:
            optimal_teams = 3  # 3-4 players per team
        elif total_players <= 16:
            optimal_teams = 4  # 4 players per team
        else:
            optimal_teams = 5  # Up to 4+ players per team
        
        # Cap number of teams at 5
        final_teams = min(5, optimal_teams)
        
        # Ensure we can make at least 2 players per team (when possible)
        while final_teams > 1 and (total_players // final_teams) < 2:
            final_teams -= 1
        
        # Ensure we don't exceed 4 players per team if we can avoid it
        while final_teams < 5 and (total_players / final_teams) > 4:
            final_teams += 1
        
        if final_teams < 1:
            return []
        
        # Use pure random or optimization algorithm based on randomization setting
        if use_randomization:
            return TeamBalancer.generate_random_teams(players, final_teams)
        else:
            return TeamBalancer.generate_optimal_teams(players, final_teams)
    
    @staticmethod
    def balance_teams_with_region(players: List[Dict], required_region: str, use_randomization: bool = False) -> List[List[Dict]]:
        """Balance players into teams ensuring each team has at least one player from the required region.
        
        Args:
            players: List of player dictionaries
            required_region: Region that must be represented in every team
            use_randomization: If True, adds more randomization to team generation
        
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
        
        # Use pure random or optimized algorithm with region constraint
        if use_randomization:
            return TeamBalancer.generate_random_teams_with_region(
                region_players, other_players, final_teams
            )
        else:
            return TeamBalancer.generate_optimal_teams_with_region(
                region_players, other_players, final_teams
            )
    
    @staticmethod
    def generate_random_teams(players: List[Dict], num_teams: int) -> List[List[Dict]]:
        """Generate completely random teams ignoring skill levels."""
        if len(players) < 2 or num_teams < 1:
            return []
        
        # Shuffle all players completely randomly
        shuffled_players = players.copy()
        random.shuffle(shuffled_players)
        
        # Distribute players randomly across teams
        teams = [[] for _ in range(num_teams)]
        
        for i, player in enumerate(shuffled_players):
            team_idx = i % num_teams
            if len(teams[team_idx]) < 4:  # Max 4 players per team
                teams[team_idx].append(player)
        
        # Remove empty teams
        return [team for team in teams if team]
    
    @staticmethod
    def generate_random_teams_with_region(
        region_players: List[Dict], 
        other_players: List[Dict], 
        num_teams: int
    ) -> List[List[Dict]]:
        """Generate completely random teams with region constraint."""
        if num_teams < 1 or not region_players:
            return []
        
        teams = [[] for _ in range(num_teams)]
        
        # Randomly assign one region player to each team
        shuffled_region = region_players.copy()
        random.shuffle(shuffled_region)
        
        for i in range(min(num_teams, len(shuffled_region))):
            teams[i].append(shuffled_region[i])
        
        # Randomly distribute remaining players
        remaining_region = shuffled_region[num_teams:]
        all_remaining = remaining_region + other_players
        random.shuffle(all_remaining)
        
        for i, player in enumerate(all_remaining):
            team_idx = i % num_teams
            if len(teams[team_idx]) < 4:  # Max 4 players per team
                teams[team_idx].append(player)
        
        # Remove empty teams
        return [team for team in teams if team]
    
    @staticmethod
    def generate_optimal_teams_with_region(
        region_players: List[Dict], 
        other_players: List[Dict], 
        num_teams: int,
        use_randomization: bool = False
    ) -> List[List[Dict]]:
        """Generate region-constrained teams with minimal variance.
        
        Args:
            use_randomization: If True, applies more aggressive randomization
        """
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
            if attempt > 0 or use_randomization:
                if use_randomization:
                    # More aggressive randomization for region players
                    tier_size = max(2, len(region_list) // 2)
                else:
                    # Standard randomization
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
            
            if attempt > 0 and all_remaining or use_randomization:
                if use_randomization:
                    # More aggressive randomization - larger tiers
                    tier_size = max(2, len(all_remaining) // 3)
                else:
                    # Standard randomization
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

class ReportMatchupModal(discord.ui.Modal, title="Report Matchup Results"):
    """A modal for reporting the results of a game."""
    def __init__(self, bot, teams: List[List[Dict]], game_id: str, original_message: discord.Message):
        super().__init__()
        self.bot = bot
        self.teams = teams
        self.game_id = game_id
        self.original_message = original_message
        
        # Create a dropdown for each team
        self.team_dropdowns = []
        for i, team in enumerate(teams):
            team_name = f"Team {i+1} ({', '.join([p['username'] for p in team])})"
            
            select_menu = discord.ui.Select(
                custom_id=f"team_result_{i}",
                placeholder=f"Result for {team_name}",
                options=[
                    discord.SelectOption(label="Win", value="win", emoji="🏆"),
                    discord.SelectOption(label="Loss", value="loss", emoji="💔"),
                    discord.SelectOption(label="Draw", value="draw", emoji="🤝"),
                ]
            )
            self.add_item(select_menu)
            self.team_dropdowns.append(select_menu)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        results = [d.values[0] for d in self.team_dropdowns]
        
        # Basic validation of results
        if results.count('win') > 1:
            await self.original_message.channel.send("❌ Error: Only one team can win.", ephemeral=True)
            return
        if results.count('win') == 0 and 'loss' in results:
            await self.original_message.channel.send("❌ Error: A loss requires a winner.", ephemeral=True)
            return
        
        try:
            # Determine ranks for TrueSkill calculation
            ranks = [0] * len(self.teams)
            for i, result in enumerate(results):
                if result == 'win':
                    ranks[i] = 0
                elif result == 'draw':
                    ranks[i] = 0
                else: # 'loss'
                    ranks[i] = 1

            # Get current ratings
            rating_groups = []
            for team in self.teams:
                team_ratings = [trueskill.Rating(mu=p['mu'], sigma=p['sigma']) for p in team]
                rating_groups.append(team_ratings)

            # Calculate new ratings
            new_rating_groups = trueskill.rate(rating_groups, ranks=ranks)

            # Update all players via the API
            update_tasks = []
            for team_idx, team in enumerate(self.teams):
                result_str = results[team_idx]
                for player_idx, player in enumerate(team):
                    new_rating = new_rating_groups[team_idx][player_idx]
                    
                    update_tasks.append(self.bot.api.update_player_stats(player['user_id'], new_rating.mu, new_rating.sigma, result_str))
                    update_tasks.append(self.bot.api.update_game_player_rating(self.game_id, player['user_id'], new_rating.mu, new_rating.sigma))

            await asyncio.gather(*update_tasks)
            
            # Respond with a summary and the new End Match button
            embed = discord.Embed(
                title="✅ Match Results Submitted",
                description="TrueSkill ratings have been updated!",
                color=discord.Color.green()
            )
            
            for i, team in enumerate(self.teams):
                old_ratings = rating_groups[i]
                new_ratings = new_rating_groups[i]
                team_text = ""
                for p_idx, player in enumerate(team):
                    old_mu = old_ratings[p_idx].mu
                    new_mu = new_ratings[p_idx].mu
                    team_text += f"**{player['username']}**: {old_mu:.1f} → {new_mu:.1f}\n"
                
                embed.add_field(name=f"Team {i+1} ({results[i].capitalize()})", value=team_text, inline=True)
            
            # Edit the original message with the new embed and view
            await self.original_message.edit(embed=embed, view=EndMatchView(self.bot))

        except Exception as e:
            await self.original_message.channel.send(f"❌ An error occurred while processing results: {e}", ephemeral=True)

class EndMatchView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
    
    @discord.ui.button(label="End Match", style=discord.ButtonStyle.red, emoji="🏁", custom_id="end_match")
    async def end_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ctx = await self.bot.get_context(interaction.message)
        await self.bot.get_command('ts cleanup').invoke(ctx)

class GameReportView(discord.ui.View):
    def __init__(self, bot, teams: List[List[Dict]], game_id: str, waiting_room_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.teams = teams
        self.game_id = game_id
        self.waiting_room_id = waiting_room_id
        self.original_message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        if self.original_message:
            embed = self.original_message.embeds[0]
            embed.set_footer(text="Match reporting timed out. Use `!ts cleanup` to reset.")
            await self.original_message.edit(embed=embed, view=None)

    @discord.ui.button(label="Report Matchup", style=discord.ButtonStyle.primary, emoji="📋", custom_id="report_matchup")
    async def report_matchup(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ReportMatchupModal(self.bot, self.teams, self.game_id, self.original_message)
        await interaction.response.send_modal(modal)

class TrueSkillBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        super().__init__(command_prefix='!', intents=intents)
        self.api = TrueSkillAPIClient(base_url=TRUESKILL_API_URL)
        self.current_teams = {}  # Store current teams by guild_id
        self.game_ids = {} # Store game_ids by guild_id
    
    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print(f'Bot is in {len(self.guilds)} guilds')

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
              "`!ts teams random` - Create completely random teams\n"
              "`!ts teams <region>` - Create teams with at least one player from region\n"
              "`!ts teams <region> random` - Create random region-based teams\n"
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
              "`!ts draw <team1_players...> vs <team2_players...>` - Record draw\n",
        inline=False
    )
    
    await ctx.send(embed=embed)

@trueskill_command.command(name='update')
async def update_player(ctx, member: discord.Member, region: str = None):
    """Update player information."""
    try:
        existing_player = await bot.api.get_player(member.id)
        if existing_player:
            await bot.api.insert_or_update_player(
                member.id, 
                member.display_name, 
                region
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
        existing_player = await bot.api.get_player(member.id)
        if existing_player:
            embed = discord.Embed(
                title="⚠️ Player Already Exists",
                description=f"{member.display_name} is already in the database. Use `!ts update` to modify.",
                color=discord.Color.orange()
            )
        else:
            await bot.api.insert_or_update_player(member.id, member.display_name, region)
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
        player = await bot.api.get_player(member.id)
        if player:
            rating = player['mu'] - 3 * player['sigma']
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
        players = await bot.api.get_all_players()
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
            leaderboard_text += f"{medal} **{player['username']}** >> {conservative_skill:.1f} ({player['games_played']} games, {winrate:.1f}% WR)\n"
        
        embed.description = leaderboard_text
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error generating leaderboard: {str(e)}")

@trueskill_command.command(name='teams')
async def create_teams(ctx, *, args: str = None):
    """Create balanced teams from players in the Waiting Room.
    
    Args:
        args: Can be 'random', '<region>', '<region> random', or empty
    """
    try:
        # Parse arguments
        region = None
        use_randomization = False
        
        if args:
            args_list = args.strip().split()
            
            if 'random' in args_list:
                use_randomization = True
                args_list = [arg for arg in args_list if arg.lower() != 'random']
                
            if args_list:
                region = args_list[0]

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
        
        members_in_room = waiting_room.members
        if len(members_in_room) < 2:
            embed = discord.Embed(
                title="❌ Not Enough Players",
                description="Need at least 2 players in the Waiting Room to create teams.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        if len(members_in_room) > 20:
            embed = discord.Embed(
                title="⚠️ Too Many Players",
                description=f"Found {len(members_in_room)} players. Maximum supported is 20 players.\n"
                          f"Only the first 20 players will be used for team balancing.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            members_in_room = members_in_room[:20]
        
        players = []
        for member in members_in_room:
            player_data = await bot.api.get_player(member.id)
            if not player_data:
                await bot.api.insert_or_update_player(member.id, member.display_name)
                player_data = await bot.api.get_player(member.id)
            players.append(player_data)
        
        if region:
            teams = TeamBalancer.balance_teams_with_region(players, region, use_randomization)
            if not teams:
                region_count = sum(1 for p in players if p.get('region', '').lower() == region.lower())
                embed = discord.Embed(
                    title="❌ Cannot Create Region-Based Teams",
                    description=f"Cannot create teams with region requirement '{region}'.\n"
                              f"Found {region_count} players from '{region}' region.",
                    color=discord.Color.red()
                )
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
            teams = TeamBalancer.balance_teams(players, use_randomization=use_randomization)
        
        if not teams:
            embed = discord.Embed(
                title="❌ Cannot Create Teams",
                description="Unable to balance players into teams.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        if use_randomization:
            title = "🎲 Random Region-Based Teams" if region else "🎲 Completely Random Teams"
            color = discord.Color.purple()
            description = (f"Created {len(teams)} completely random teams from {len(players)} players:\n"
                           f"🎯 Teams are randomized (ignoring TrueSkill ratings)\n"
                           f"📊 Max 4 players per team | Max 5 teams total")
            if region:
                 description = (f"Created {len(teams)} completely random teams from {len(players)} players:\n"
                                f"🌍 Each team has at least one '{region}' player\n"
                                f"🎯 Teams are randomized (ignoring TrueSkill ratings)\n"
                                f"📊 Max 4 players per team | Max 5 teams total")
        else:
            title = "⚖️ Region-Based Balanced Teams" if region else "⚖️ Optimally Balanced Teams"
            color = discord.Color.green()
            description = (f"Created {len(teams)} optimally balanced teams from {len(players)} players:\n"
                           f"📊 Max 4 players per team | Max 5 teams total")
            if region:
                 description = (f"Created {len(teams)} balanced teams from {len(players)} players:\n"
                                f"🌍 Each team has at least one '{region}' player\n"
                                f"📊 Max 4 players per team | Max 5 teams total")

        embed = discord.Embed(title=title, description=description, color=color)

        for i, team in enumerate(teams, 1):
            team_text = ""
            for player in team:
                if use_randomization:
                    player_region = player.get('region', 'Unknown')
                    region_indicator = " 🌍" if region and player_region.lower() == region.lower() else ""
                    team_text += f"• {player['username']} [{player_region}]{region_indicator}\n"
                else:
                    conservative_skill = TeamBalancer.get_player_skill(player)
                    player_region = player.get('region', 'Unknown')
                    region_indicator = " 🌍" if region and player_region.lower() == region.lower() else ""
                    team_text += f"• {player['username']} ({conservative_skill:.1f}) [{player_region}]{region_indicator}\n"
            
            if use_randomization:
                field_name = f"Team {i} ({len(team)} players)"
            else:
                team_strength = TeamBalancer.calculate_team_average(team)
                field_name = f"Team {i} (Avg: {team_strength:.1f})"
            embed.add_field(name=field_name, value=team_text, inline=True)

        if not use_randomization:
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
        
        bot.current_teams[ctx.guild.id] = teams
        
        teams_user_ids = [[p['user_id'] for p in team] for team in teams]
        game_id = await bot.api.record_game(teams_user_ids)
        bot.game_ids[ctx.guild.id] = game_id

        # Use the new view with the delayed button
        view = GameReportView(bot, teams, game_id, waiting_room.id)
        message = await ctx.send(embed=embed, view=view)
        view.original_message = message
        
    except Exception as e:
        await ctx.send(f"❌ Error creating teams: {str(e)}")

@trueskill_command.command(name='cleanup')
async def cleanup_teams(ctx):
    """Move all players back to Waiting Room and delete temporary channels."""
    try:
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
        
        temp_channels = []
        players_moved = 0
        channels_deleted = 0
        
        for channel in ctx.guild.voice_channels:
            if (channel.name.startswith("Team ") and " - Game " in channel.name) or \
               ("team" in channel.name.lower() and channel != waiting_room and 
                channel.category == waiting_room.category):
                temp_channels.append(channel)
        
        for channel in temp_channels:
            for member in list(channel.members):
                try:
                    await member.move_to(waiting_room)
                    players_moved += 1
                except discord.HTTPException:
                    pass
        
        for channel in temp_channels:
            try:
                await channel.delete()
                channels_deleted += 1
            except discord.HTTPException:
                pass
        
        if ctx.guild.id in bot.current_teams:
            del bot.current_teams[ctx.guild.id]
        if ctx.guild.id in bot.game_ids:
            del bot.game_ids[ctx.guild.id]
        
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

# Error handlers
@update_player.error
@insert_player.error
@view_player.error
async def player_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Please mention a valid user.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing required argument. Please check the command usage.")

@cleanup_teams.error
async def cleanup_command_error(ctx, error):
    await ctx.send(f"❌ Error during cleanup: {str(error)}")

@trueskill_command.command(name='win')
async def record_win(ctx, member: discord.Member):
    """Record a win for a specific user."""
    try:
        player = await bot.api.get_player(member.id)
        if not player:
            await bot.api.insert_or_update_player(member.id, member.display_name)
            player = await bot.api.get_player(member.id)
        
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        new_rating = trueskill.rate([[old_rating], [trueskill.Rating()]])[0][0]
        
        await bot.api.update_player_stats(member.id, new_rating.mu, new_rating.sigma, 'win')
        
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
        player = await bot.api.get_player(member.id)
        if not player:
            await bot.api.insert_or_update_player(member.id, member.display_name)
            player = await bot.api.get_player(member.id)
        
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        new_rating = trueskill.rate([[old_rating], [trueskill.Rating()]])[1][0]
        
        await bot.api.update_player_stats(member.id, new_rating.mu, new_rating.sigma, 'loss')
        
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
        player = await bot.api.get_player(member.id)
        if not player:
            await bot.api.insert_or_update_player(member.id, member.display_name)
            player = await bot.api.get_player(member.id)
        
        old_rating = trueskill.Rating(mu=player['mu'], sigma=player['sigma'])
        rating_groups = [[old_rating], [trueskill.Rating(mu=player['mu'], sigma=player['sigma'])]]
        new_rating_groups = trueskill.rate(rating_groups, ranks=[0, 0])
        new_rating = new_rating_groups[0][0]
        
        await bot.api.update_player_stats(member.id, new_rating.mu, new_rating.sigma, 'draw')
        
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

# This section of commands is no longer used due to the new button-driven flow.
# They are kept here for reference but can be removed.
@trueskill_command.command(name='teamwin')
async def record_team_win(ctx, team_number: int):
    await ctx.send("This command is deprecated. Please use the 'Report Matchup' button to record game results.")

@trueskill_command.command(name='teamloss')
async def record_team_loss(ctx, team_number: int):
    await ctx.send("This command is deprecated. Please use the 'Report Matchup' button to record game results.")

@trueskill_command.command(name='teamdraw')
async def record_team_draw(ctx, *team_numbers: int):
    await ctx.send("This command is deprecated. Please use the 'Report Matchup' button to record game results.")

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
