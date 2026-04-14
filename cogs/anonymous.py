import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from config import COLOUR_MAIN, COLOUR_LB, COLOUR_CONFIRM


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_GUESS_TIMEOUT_HOURS = 24
DEFAULT_ANON_POINTS = 10  # points for surviving 3 wrong guesses
DEFAULT_GUESS_POINTS = 5  # points for a correct guess


# ── Answer modal ──────────────────────────────────────────────────────────────


class AnswerModal(discord.ui.Modal, title="Answer Anonymously"):
    answer = discord.ui.TextInput(
        label="Your answer",
        style=discord.TextStyle.paragraph,
        placeholder="Write your answer here (more than 3 words)...",
        min_length=1,
        max_length=500,
    )

    def __init__(self, cog, question: str, question_id: str):
        super().__init__()
        self.cog = cog
        self.question = question
        self.question_id = question_id

    async def on_submit(self, interaction: discord.Interaction):
        text = self.answer.value.strip()

        if not text:
            await interaction.response.send_message(
                "⚠️ Your answer cannot be empty. Try again with `/answer`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.cog._post_answer(interaction, self.question, self.question_id, text)


# ── Guess modal (shown when button is pressed) ────────────────────────────────


class GuessMemberSelect(discord.ui.Select):
    def __init__(self, members: list[discord.Member]):
        options = [
            discord.SelectOption(
                label=m.display_name,
                value=str(m.id),
                emoji="🌙",
            )
            for m in members[:25]
        ]
        super().__init__(
            placeholder="Who do you think answered?",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog._handle_guess(
            interaction,
            self.view.round_id,
            int(self.values[0]),
        )


class GuessMemberView(discord.ui.View):
    """Ephemeral dropdown shown when the Guess button is pressed."""

    def __init__(self, cog, round_id: str, members: list[discord.Member]):
        super().__init__(timeout=60)
        self.cog = cog
        self.round_id = round_id
        self.add_item(GuessMemberSelect(members))


# ── Main view with Guess button ───────────────────────────────────────────────


class GuessView(discord.ui.View):
    def __init__(self, cog, round_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.round_id = round_id
        # Set custom_id on the button dynamically so it survives restarts
        for item in self.children:
            if hasattr(item, "custom_id"):
                item.custom_id = f"anon_guess:{round_id}"

    @discord.ui.button(
        label="🌙 Make a Guess",
        style=discord.ButtonStyle.secondary,
        custom_id="anon_guess:placeholder",
    )
    async def guess_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        from bson import ObjectId

        # Extract round_id from custom_id
        round_id = button.custom_id.split(":", 1)[1]
        round_doc = await self.cog.bot.anon_rounds_col.find_one(
            {"_id": ObjectId(round_id)}
        )
        if not round_doc or round_doc["closed"]:
            await interaction.response.send_message(
                "🌙 This round is already closed.",
                ephemeral=True,
            )
            return

        settings = await self.cog._get_settings(interaction.guild_id)
        guesser_role_id = settings.get("anon_guesser_role_id")

        # Check guesser role
        if guesser_role_id and guesser_role_id not in [
            r.id for r in interaction.user.roles
        ]:
            role = interaction.guild.get_role(guesser_role_id)
            await interaction.response.send_message(
                f"⚠️ You need the **{role.name if role else 'guesser'}** role to guess.",
                ephemeral=True,
            )
            return

        # Check they haven't already guessed
        if any(g["user_id"] == interaction.user.id for g in round_doc["guesses"]):
            await interaction.response.send_message(
                "🌙 You've already made a guess for this answer.",
                ephemeral=True,
            )
            return

        # Check they're not the answerer
        if interaction.user.id == round_doc["answerer_id"]:
            await interaction.response.send_message(
                "🌙 You can't guess your own answer!",
                ephemeral=True,
            )
            return

        # Build member list for the ephemeral dropdown
        if guesser_role_id:
            guesser_role = interaction.guild.get_role(guesser_role_id)
            members = [
                m
                for m in interaction.guild.members
                if not m.bot and m.id != interaction.user.id and guesser_role in m.roles
            ]
        else:
            members = [
                m
                for m in interaction.guild.members
                if not m.bot and m.id != interaction.user.id
            ]

        view = GuessMemberView(self.cog, round_id, members)
        await interaction.response.send_message(
            "*who do you think answered?*",
            view=view,
            ephemeral=True,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────


class Anonymous(commands.Cog):
    """Anonymous Q&A minigame."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # round_id -> asyncio task handle
        self._timeout_tasks: dict[str, asyncio.Task] = {}

    async def cog_load(self):
        """Re-register persistent views for all open rounds on startup."""
        open_rounds = await self.bot.anon_rounds_col.find({"closed": False}).to_list(
            length=200
        )
        for round_doc in open_rounds:
            round_id = str(round_doc["_id"])
            self.bot.add_view(GuessView(self, round_id))
        if open_rounds:
            print(
                f"[Anonymous] Re-registered {len(open_rounds)} persistent guess views"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_settings(self, guild_id: int) -> dict:
        doc = await self.bot.settings_col.find_one({"guild_id": guild_id})
        return doc or {}

    async def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── /answer ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="answer", description="Answer today's anonymous question"
    )
    async def answer(self, interaction: discord.Interaction):
        settings = await self._get_settings(interaction.guild_id)

        # Check answer channel is configured
        if not settings.get("anon_channel_id"):
            await interaction.response.send_message(
                "⚠️ No answer channel has been set. Ask an admin to run `/setanswerchannel`.",
                ephemeral=True,
            )
            return

        # Check questions exist
        questions = await self.bot.questions_col.find(
            {"guild_id": interaction.guild_id}
        ).to_list(length=200)
        if not questions:
            await interaction.response.send_message(
                "⚠️ No questions have been added yet. Ask an admin to run `/addquestion`.",
                ephemeral=True,
            )
            return

        # Check member hasn't answered today
        today = await self._today_str()
        existing = await self.bot.anon_rounds_col.find_one(
            {
                "guild_id": interaction.guild_id,
                "answerer_id": interaction.user.id,
                "date": today,
            }
        )
        if existing:
            await interaction.response.send_message(
                "🌙 You've already answered today's question. Come back tomorrow!",
                ephemeral=True,
            )
            return

        # Pick a random question
        question = random.choice(questions)
        question_text = question["text"]
        question_id = str(question["_id"])

        # Show the modal
        modal = AnswerModal(self, question_text, question_id)
        modal.title = "Answer Anonymously"
        # Inject the question as the modal description via the label
        modal.answer.label = question_text[:45] + (
            "..." if len(question_text) > 45 else ""
        )
        await interaction.response.send_modal(modal)

    async def _post_answer(
        self,
        interaction: discord.Interaction,
        question: str,
        question_id: str,
        answer_text: str,
    ):
        settings = await self._get_settings(interaction.guild_id)
        channel = interaction.guild.get_channel(settings["anon_channel_id"])
        timeout_hrs = settings.get(
            "anon_guess_timeout_hours", DEFAULT_GUESS_TIMEOUT_HOURS
        )
        today = await self._today_str()

        if not channel:
            await interaction.followup.send(
                "⚠️ The answer channel no longer exists. Ask an admin to run `/setanswerchannel`.",
                ephemeral=True,
            )
            return

        # Build and post the embed
        embed = discord.Embed(
            title="🌙 Anonymous Answer",
            color=COLOUR_MAIN,
        )
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer", value=f"*{answer_text}*", inline=False)
        embed.set_footer(
            text=f"Guess who answered - {timeout_hrs}h remaining - 3 wrong guesses = answerer gets points"
        )

        # Save the round first to get the round_id
        round_doc = {
            "guild_id": interaction.guild_id,
            "message_id": 0,  # updated after send
            "channel_id": channel.id,
            "answerer_id": interaction.user.id,
            "question": question,
            "answer": answer_text,
            "date": today,
            "guesses": [],
            "wrong_count": 0,
            "closed": False,
            "revealed": False,
        }
        result = await self.bot.anon_rounds_col.insert_one(round_doc)
        round_id = str(result.inserted_id)

        view = GuessView(self, round_id)
        msg = await channel.send(embed=embed, view=view)

        # Update message_id now we have it
        await self.bot.anon_rounds_col.update_one(
            {"_id": result.inserted_id},
            {"$set": {"message_id": msg.id}},
        )

        # Register the view with bot so it persists across restarts
        self.bot.add_view(view, message_id=msg.id)

        # Schedule timeout
        task = asyncio.create_task(
            self._close_round_after(round_id, interaction.guild_id, timeout_hrs)
        )
        self._timeout_tasks[round_id] = task

        await interaction.followup.send(
            "🌙 Your answer has been posted anonymously. Good luck!",
            ephemeral=True,
        )

    # ── Guess handler ─────────────────────────────────────────────────────────

    async def _handle_guess(
        self,
        interaction: discord.Interaction,
        round_id: str,
        guessed_id: int,
    ):
        from bson import ObjectId

        round_doc = await self.bot.anon_rounds_col.find_one({"_id": ObjectId(round_id)})
        if not round_doc or round_doc["closed"]:
            await interaction.response.send_message(
                "🌙 This round is already closed.",
                ephemeral=True,
            )
            return

        settings = await self._get_settings(interaction.guild_id)
        guesser_role_id = settings.get("anon_guesser_role_id")

        # Check guesser role
        if guesser_role_id and guesser_role_id not in [
            r.id for r in interaction.user.roles
        ]:
            role = interaction.guild.get_role(guesser_role_id)
            await interaction.response.send_message(
                f"⚠️ You need the **{role.name if role else 'guesser'}** role to guess.",
                ephemeral=True,
            )
            return

        # Check they haven't already guessed this round
        if any(g["user_id"] == interaction.user.id for g in round_doc["guesses"]):
            await interaction.response.send_message(
                "🌙 You've already made a guess for this answer.",
                ephemeral=True,
            )
            return

        # Check they're not guessing themselves
        if interaction.user.id == round_doc["answerer_id"]:
            await interaction.response.send_message(
                "🌙 You can't guess your own answer!",
                ephemeral=True,
            )
            return

        correct = guessed_id == round_doc["answerer_id"]

        # Record the guess
        await self.bot.anon_rounds_col.update_one(
            {"_id": ObjectId(round_id)},
            {
                "$push": {
                    "guesses": {"user_id": interaction.user.id, "correct": correct}
                },
                "$inc": {"wrong_count": 0 if correct else 1},
            },
        )

        if correct:
            # Award guesser points and reveal
            guess_pts = settings.get("anon_guess_points", DEFAULT_GUESS_POINTS)
            await self.bot.users_col.update_one(
                {"user_id": interaction.user.id, "guild_id": interaction.guild_id},
                {"$inc": {"points": guess_pts}},
            )
            await self._close_round(
                round_id,
                interaction.guild_id,
                reveal=True,
                winner_id=interaction.user.id,
            )
            await interaction.response.send_message(
                f"🎉 Correct! You earned **{guess_pts}** dream points!",
                ephemeral=True,
            )
        else:
            # Refresh doc to get updated wrong count
            round_doc = await self.bot.anon_rounds_col.find_one(
                {"_id": ObjectId(round_id)}
            )
            wrong = round_doc["wrong_count"]
            guessed_member = interaction.guild.get_member(guessed_id)
            guessed_name = (
                guessed_member.display_name if guessed_member else "that person"
            )

            if wrong >= 3:
                # Award answerer and close without reveal
                anon_pts = settings.get("anon_points", DEFAULT_ANON_POINTS)
                await self.bot.users_col.update_one(
                    {
                        "user_id": round_doc["answerer_id"],
                        "guild_id": interaction.guild_id,
                    },
                    {"$inc": {"points": anon_pts}},
                )
                await self._close_round(round_id, interaction.guild_id, reveal=False)
                await interaction.response.send_message(
                    f"❌ Wrong - it wasn't **{guessed_name}**. "
                    f"3 wrong guesses reached - the answerer earned **{anon_pts}** dream points and their identity stays secret!",
                    ephemeral=True,
                )
            else:
                remaining = 3 - wrong
                await interaction.response.send_message(
                    f"❌ Wrong - it wasn't **{guessed_name}**. "
                    f"**{remaining}** wrong guess{'es' if remaining != 1 else ''} remaining.",
                    ephemeral=True,
                )

    async def _close_round(
        self,
        round_id: str,
        guild_id: int,
        reveal: bool,
        winner_id: int | None = None,
    ):
        from bson import ObjectId

        round_doc = await self.bot.anon_rounds_col.find_one({"_id": ObjectId(round_id)})
        if not round_doc or round_doc["closed"]:
            return

        await self.bot.anon_rounds_col.update_one(
            {"_id": ObjectId(round_id)},
            {"$set": {"closed": True, "revealed": reveal}},
        )

        # Cancel timeout task if still running
        task = self._timeout_tasks.pop(round_id, None)
        if task and not task.done():
            task.cancel()

        # Update the original message
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(round_doc["channel_id"]) if guild else None
        if not channel:
            return

        try:
            msg = await channel.fetch_message(round_doc["message_id"])
        except (discord.NotFound, discord.Forbidden):
            return

        answerer = guild.get_member(round_doc["answerer_id"])
        answerer_name = answerer.display_name if answerer else "someone"

        embed = discord.Embed(
            title="🌙 Anonymous Answer - Closed",
            color=COLOUR_CONFIRM if reveal else COLOUR_LB,
        )
        embed.add_field(name="Question", value=round_doc["question"], inline=False)
        embed.add_field(name="Answer", value=f"*{round_doc['answer']}*", inline=False)

        if reveal:
            winner = guild.get_member(winner_id) if winner_id else None
            embed.add_field(
                name="Answered by",
                value=f"{answerer.mention if answerer else answerer_name}",
                inline=True,
            )
            if winner:
                embed.add_field(
                    name="Correct guess by",
                    value=winner.mention,
                    inline=True,
                )
            embed.set_footer(text=f"Correctly guessed - Reverie - {guild.name}")
        else:
            total_wrong = round_doc["wrong_count"]
            embed.add_field(
                name="Result",
                value=f"*{total_wrong} wrong guess{'es' if total_wrong != 1 else ''} - identity remains a secret*",
                inline=False,
            )
            embed.set_footer(text=f"Round closed - Reverie - {guild.name}")

        await msg.edit(embed=embed, view=None)

    async def _close_round_after(self, round_id: str, guild_id: int, hours: int):
        await asyncio.sleep(hours * 3600)
        from bson import ObjectId

        round_doc = await self.bot.anon_rounds_col.find_one({"_id": ObjectId(round_id)})
        if round_doc and not round_doc["closed"]:
            # Time ran out - close without revealing (no points awarded)
            await self._close_round(round_id, guild_id, reveal=False)

    # ── Admin setup commands ──────────────────────────────────────────────────

    @app_commands.command(
        name="addquestion", description="[Admin] Add a question to the anonymous pool"
    )
    @app_commands.describe(question="The question to add")
    @app_commands.default_permissions(administrator=True)
    async def addquestion(self, interaction: discord.Interaction, question: str):
        await self.bot.questions_col.insert_one(
            {
                "guild_id": interaction.guild_id,
                "text": question.strip(),
            }
        )
        await interaction.response.send_message(
            f"✅ Question added: *{question.strip()}*",
            ephemeral=True,
        )

    @app_commands.command(
        name="removequestion",
        description="[Admin] Remove a question from the anonymous pool",
    )
    @app_commands.describe(question="The exact question text to remove")
    @app_commands.default_permissions(administrator=True)
    async def removequestion(self, interaction: discord.Interaction, question: str):
        result = await self.bot.questions_col.delete_one(
            {
                "guild_id": interaction.guild_id,
                "text": {"$regex": f"^{question.strip()}$", "$options": "i"},
            }
        )
        if result.deleted_count:
            await interaction.response.send_message(
                "✅ Question removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Question not found.", ephemeral=True
            )

    @app_commands.command(
        name="listquestions", description="[Admin] List all questions in the pool"
    )
    @app_commands.default_permissions(administrator=True)
    async def listquestions(self, interaction: discord.Interaction):
        questions = await self.bot.questions_col.find(
            {"guild_id": interaction.guild_id}
        ).to_list(length=100)
        if not questions:
            await interaction.response.send_message(
                "No questions added yet.", ephemeral=True
            )
            return
        lines = [f"`{i+1}.` {q['text']}" for i, q in enumerate(questions)]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
        )

    @app_commands.command(
        name="setanswerchannel",
        description="[Admin] Set the channel where anonymous answers are posted",
    )
    @app_commands.describe(channel="The channel to post answers in")
    @app_commands.default_permissions(administrator=True)
    async def setanswerchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"anon_channel_id": channel.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Anonymous answers will be posted in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setguessingrole",
        description="[Admin] Set the role required to guess in the anonymous game",
    )
    @app_commands.describe(role="The role that can make guesses")
    @app_commands.default_permissions(administrator=True)
    async def setguessingrole(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"anon_guesser_role_id": role.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ **{role.name}** can now make guesses.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setguesstimeout",
        description="[Admin] Set how many hours guessing stays open",
    )
    @app_commands.describe(hours="Number of hours before guessing closes")
    @app_commands.default_permissions(administrator=True)
    async def setguesstimeout(self, interaction: discord.Interaction, hours: int):
        if hours < 1 or hours > 168:
            await interaction.response.send_message(
                "⚠️ Hours must be between 1 and 168 (1 week).",
                ephemeral=True,
            )
            return
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"anon_guess_timeout_hours": hours}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Guessing will close after **{hours}** hour{'s' if hours != 1 else ''}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setanonymouspoints",
        description="[Admin] Set points awarded to answerer for 3 wrong guesses",
    )
    @app_commands.describe(amount="Points to award")
    @app_commands.default_permissions(administrator=True)
    async def setanonymouspoints(self, interaction: discord.Interaction, amount: int):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"anon_points": amount}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Answerer will earn **{amount}** dream points for surviving 3 wrong guesses.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setguesspoints",
        description="[Admin] Set points awarded for a correct guess",
    )
    @app_commands.describe(amount="Points to award")
    @app_commands.default_permissions(administrator=True)
    async def setguesspoints(self, interaction: discord.Interaction, amount: int):
        await self.bot.settings_col.update_one(
            {"guild_id": interaction.guild_id},
            {"$set": {"anon_guess_points": amount}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"✅ Correct guessers will earn **{amount}** dream points.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Anonymous(bot))
