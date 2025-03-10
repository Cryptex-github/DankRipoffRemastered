from __future__ import annotations

import asyncio
import re
from functools import partial
from textwrap import dedent
from typing import Any, TYPE_CHECKING

import discord

from app.core import (
    BAD_ARGUMENT,
    Cog,
    Context,
    EDIT,
    NO_EXTRA,
    REPLY,
    command,
    lock_transactions,
    simple_cooldown,
    user_max_concurrency,
)
from app.data.items import Item, Items
from app.data.recipes import Recipe, Recipes
from app.util.common import cutoff, get_by_key, image_url_from_emoji, walk_collection
from app.util.converters import (
    BUY,
    BankTransaction,
    CaseInsensitiveMemberConverter,
    DEPOSIT,
    DROP,
    DropAmount,
    ItemAndQuantityConverter,
    RecipeConverter,
    SELL,
    USE,
    WITHDRAW,
    get_amount,
    query_item,
    query_recipe,
)
from app.util.pagination import FieldBasedFormatter, Paginator
from app.util.views import UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.database import UserRecord


class DropView(discord.ui.View):
    def __init__(self, ctx: Context) -> None:
        super().__init__(timeout=120)

        self.ctx: Context = ctx
        self.winner: discord.Member | None = None

        self._lock: asyncio.Lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.ctx.author:
            await interaction.response.send_message("You cannot claim your own drop, it's too late now!", ephemeral=True)

            return False

        return True

    @discord.ui.button(label='Claim!', style=discord.ButtonStyle.success)
    async def claim(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if self.winner:
            await interaction.response.send_message('This drop has already been claimed!', ephemeral=True)

        async with self._lock:
            self.winner = interaction.user
            button.disabled = True

            await interaction.response.edit_message(view=self)
            self.stop()

    async def on_timeout(self) -> None:
        assert self.children

        child = self.children[0]
        assert isinstance(child, discord.ui.Button)

        child.disabled = True


class RecipeSelect(discord.ui.Select['RecipeView']):
    def __init__(self, default: Recipe | None = None) -> None:
        super().__init__(
            placeholder='Choose a recipe...',
            options=[
                discord.SelectOption(
                    label=recipe.name,
                    value=recipe.key,
                    emoji=recipe.emoji,
                    description=cutoff(recipe.description, max_length=50, exact=True),
                    default=default == recipe,
                )
                for recipe in walk_collection(Recipes, Recipe)
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            recipe = get_by_key(Recipes, self.values[0])
        except (KeyError, IndexError):
            return await interaction.response.send_message('Could not resolve that recipe for some reason.', ephemeral=True)

        self.view.current = recipe
        self.view.update()
        await interaction.response.edit_message(embed=self.view.build_embed(), view=self.view)


class RecipeView(UserView):
    REPLACE_REGEX: re.Pattern[str] = re.compile(r'[^\s]')

    def __init__(self, ctx: Context, record: UserRecord, default: Recipe | None = None) -> None:
        self.ctx: Context = ctx
        self.record: UserRecord = record

        self.current: Recipe = default or next(walk_collection(Recipes, Recipe))
        self.input_lock: asyncio.Lock = asyncio.Lock()

        super().__init__(ctx.author)
        self.add_item(RecipeSelect(default=default))

        self.update()

    @property
    def discovered(self) -> bool:
        return self.current.key in self.record.discovered_recipes

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.current.name, description=self.current.description, color=Colors.primary, timestamp=self.ctx.now,
        )
        embed.set_author(name='Recipes', icon_url=self.ctx.author.avatar.url)
        embed.set_thumbnail(url=image_url_from_emoji(self.current.emoji))

        if not self.discovered:
            embed.set_footer(text='You have not discovered this recipe yet!')

        embed.add_field(name='General', value=dedent(f"""
            **Name:**: {self.current.name}
            **Query Key: `{self.current.key}`**
            **Price:** {Emojis.coin} {self.current.price:,}
        """), inline=False)

        embed.add_field(name='Ingredients', value='\n'.join(
            (f'{item.display_name} x{quantity}' for item, quantity in self.current.ingredients.items())
            if self.discovered
            else (
                f'{self.REPLACE_REGEX.sub("?", item.name)} x{quantity}' for item, quantity in self.current.ingredients.items()
            )
        ))

        return embed

    def update(self) -> None:
        amount = self._get_max()
        toggle = self.discovered and amount > 0

        for child in self.children:
            if child is self.stop_button or not isinstance(child, discord.ui.Button):
                continue

            child.disabled = not toggle
            child.style = discord.ButtonStyle.primary if self.discovered else discord.ButtonStyle.secondary

        if self.discovered:
            self.craft_max.label = f'Craft Max ({amount:,})'
        else:
            self.craft_max.label = 'Craft Max'

    async def _craft(self, amount: int = 1, *, interaction: discord.Interaction = None) -> Any:
        respond_error = partial(interaction.response.send_message, ephemeral=True) if interaction else self.ctx.reply
        respond = interaction.response.send_message if interaction else self.ctx.reply

        if not self.discovered:  # Just in case the user somehow manages to click the button
            return await respond_error('You have not discovered this recipe yet!')

        extra = f' ({Emojis.coin} **{self.current.price * amount:,}** for {amount})' if amount > 1 else ''

        if self.record.wallet < self.current.price * amount:
            return await respond_error(
                f'Insufficient funds: Crafting one of this item costs {Emojis.coin} **{self.current.price:,}**{extra}, '
                f'you only have {Emojis.coin} **{self.record.wallet:,}**.',
            )

        manager = self.record.inventory_manager
        quantity_of = manager.cached.quantity_of

        if any(quantity_of(item) < quantity * amount for item, quantity in self.current.ingredients.items()):
            extra = ', maybe try a lower amount?' if amount > 1 else ''

            return await respond_error(
                f"You don't have enough of the required ingredients to craft this recipe{extra}",
            )

        async with self.record.db.acquire() as conn:
            await self.record.add(wallet=-self.current.price * amount, connection=conn)

            for item, quantity in self.current.ingredients.items():
                await manager.add_item(item, -quantity * amount, connection=conn)

            for item, quantity in self.current.result.items():
                await manager.add_item(item, quantity * amount, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=self.ctx.now)
        embed.set_author(name='Crafted Successfully', icon_url=self.ctx.author.avatar.url)

        embed.add_field(
            name='Crafted',
            value='\n'.join(f'{item.display_name} x{quantity * amount:,}' for item, quantity in self.current.result.items()),
            inline=False
        )

        embed.add_field(
            name='Ingredients Used',
            value=f'{Emojis.coin} {self.current.price * amount:,}\n' + '\n'.join(
                f'{item.display_name} x{quantity * amount:,}' for item, quantity in self.current.ingredients.items()
            ),
            inline=False,
        )
        await respond(embed=embed)

    def _get_max(self) -> int:
        inventory = self.record.inventory_manager

        item_max = min(inventory.cached.quantity_of(item) // quantity for item, quantity in self.current.ingredients.items())
        return min(item_max, self.record.wallet // self.current.price)

    @discord.ui.button(label='Craft One', style=discord.ButtonStyle.primary, row=1)
    async def craft_one(self, _, interaction: discord.Interaction) -> None:
        await self._craft(1, interaction=interaction)

    @discord.ui.button(label='Craft Max', style=discord.ButtonStyle.primary, row=1)
    async def craft_max(self, _, interaction: discord.Interaction) -> None:
        await self._craft(self._get_max(), interaction=interaction)

    @discord.ui.button(label='Craft Custom', style=discord.ButtonStyle.primary, row=1)
    async def craft_custom(self, _, interaction: discord.Interaction) -> Any:
        async with self.input_lock:
            await interaction.response.send_message(
                'How many of this item/recipe do you want to craft? Send a valid quantity in chat, e.g. "3" or "half".',
            )

            try:
                response = await self.ctx.bot.wait_for(
                    'message', timeout=30, check=lambda m: m.author == interaction.user,
                )
            except asyncio.TimeoutError:
                return await self.ctx.reply("You took too long to respond, cancelling.")

            maximum = self._get_max()
            try:
                await self._craft(get_amount(maximum, 1, maximum, response.content))
            except Exception as e:
                await self.ctx.reply(f'Error: {e.__class__.__name__}')

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True

            if child is not button and isinstance(child, discord.ui.Button):
                child.style = discord.ButtonStyle.secondary

        self.stop()
        await interaction.response.edit_message(view=self)


class Transactions(Cog):
    """Commands that handle transactions between the bank or other users."""

    # noinspection PyTypeChecker
    @command(aliases={"w", "with", "wd"})
    @simple_cooldown(1, 8)
    @lock_transactions
    async def withdraw(self, ctx: Context, *, amount: BankTransaction(WITHDRAW)) -> Any:
        """Withdraw coins from your bank."""
        data = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.typing():
            await data.add(wallet=amount, bank=-amount)

        embed = discord.Embed(color=Colors.primary)
        embed.set_author(name=f"Successful Transaction: {ctx.author}", icon_url=ctx.author.avatar)

        embed.description = f"Withdrew {Emojis.coin} **{amount:,}** from your bank."
        embed.add_field(name="Updated Balance", value=dedent(f"""
            Wallet: {Emojis.coin} **{data.wallet:,}**
            Bank: {Emojis.coin} **{data.bank:,}**
        """))

        return embed, REPLY

    # noinspection PyTypeChecker
    @command(aliases={"d", "dep"})
    @simple_cooldown(1, 8)
    @lock_transactions
    async def deposit(self, ctx: Context, *, amount: BankTransaction(DEPOSIT)) -> Any:
        """Deposit coins from your wallet into your bank."""
        data = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.typing():
            await data.add(wallet=-amount, bank=amount)

        embed = discord.Embed(color=Colors.primary)
        embed.set_author(name=f"Successful Transaction: {ctx.author}", icon_url=ctx.author.avatar)

        embed.description = f"Deposited {Emojis.coin} **{amount:,}** into your bank."
        embed.add_field(name="Updated Balance", value=dedent(f"""
            Wallet: {Emojis.coin} **{data.wallet:,}**
            Bank: {Emojis.coin} **{data.bank:,}**
        """))

        return embed, REPLY

    @command(aliases={"store", "market", "sh", "iteminfo", "ii"})
    @simple_cooldown(1, 6)
    async def shop(self, ctx: Context, *, item: query_item = None) -> tuple[Paginator | discord.Embed, Any]:
        """View the item shop, or view information on a specific item."""
        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        record = await ctx.db.get_user_record(ctx.author.id)

        inventory = record.inventory_manager
        await inventory.wait()

        if not item:
            fields = []

            for i in walk_collection(Items, Item):
                if not i.buyable:
                    continue

                embed.title = 'Item Shop'
                embed.description = f'To buy an item, see `{ctx.clean_prefix}buy`.\nTo view information on an item, see `{ctx.clean_prefix}iteminfo`.'

                comment = '*You cannot afford this item.*\n' if i.price > record.wallet else ''
                owned = inventory.cached.quantity_of(i)
                owned = f'(You own {owned:,})' if owned else ''

                description = cutoff(i.description, max_length=100)

                fields.append({
                    'name': f'• {i.display_name} — {Emojis.coin} {i.price:,} {owned}',
                    'value': comment + description,
                    'inline': False,
                })

            return Paginator(ctx, FieldBasedFormatter(embed, fields, per_page=5)), REPLY

        item: Item

        owned = inventory.cached.quantity_of(item)

        embed.title = f'{item.display_name} ({owned} owned)'
        embed.description = item.description

        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        embed.add_field(name='General', value=dedent(f"""
            Name: {item.get_display_name(bold=True)}
            Query Key: **`{item.key}`**
            Type: **{item.type.name.title()}**
            Rarity: **{item.rarity.name.title()}**
        """))

        embed.add_field(name='Pricing', value=dedent(f"""
            Buy Price: {Emojis.coin} **{item.price:,}**
            {
                f'Sell Value: {Emojis.coin} **{item.sell:,}**' if item.sellable else ''
            }
        """))

        embed.add_field(name='Flexibility', value=dedent(f"""
            Buyable? **{self._bool_to_human(item.buyable)}**
            Sellable? **{self._bool_to_human(item.sellable)}**
            Usable? **{self._bool_to_human(item.usable)}**
            Removable? **{self._bool_to_human(item.removable)}**
        """), inline=False)

        return embed, REPLY

    @staticmethod
    def _bool_to_human(b: bool) -> str:
        return 'Yes' if b else 'No'

    @command(alias='purchase')
    @simple_cooldown(3, 8)
    @user_max_concurrency(1)
    @lock_transactions
    async def buy(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(BUY)) -> tuple[discord.Embed | str, Any]:
        """Buy items!"""
        item, quantity = item_and_quantity
        price = item.price * quantity

        if not await ctx.confirm(
            f'Are you sure you want to buy {item.get_sentence_chunk(quantity)} for {Emojis.coin} **{price:,}**?',
            delete_after=True,
            reference=ctx.message,
        ):
            return 'Cancelled purchase.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = record.inventory_manager

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.5, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.5, connection=conn)

            await record.add(wallet=-price, connection=conn)
            await inventory.add_item(item, quantity, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You bought {item.get_sentence_chunk(quantity)} for {Emojis.coin} **{price:,}** coins.'
        embed.set_author(name=f'Successful Purchase: {ctx.author}', icon_url=ctx.author.avatar.url)
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        return embed, REPLY

    @command(alias='s')
    @simple_cooldown(3, 8)
    @user_max_concurrency(1)
    @lock_transactions
    async def sell(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(SELL)) -> tuple[discord.Embed | str, Any]:
        """Sell items from your inventory for coins."""
        item, quantity = item_and_quantity
        value = item.sell * quantity

        if not await ctx.confirm(
            f'Are you sure you want to sell {item.get_sentence_chunk(quantity)} in exchange for {Emojis.coin} **{value:,}**?',
            delete_after=True,
            reference=ctx.message,
        ):
            return 'Cancelled transaction.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = record.inventory_manager

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.4, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            await record.add(wallet=value, connection=conn)
            await inventory.add_item(item, -quantity, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You sold {item.get_sentence_chunk(quantity)} in exchange for {Emojis.coin} **{value:,}** coins.'
        embed.set_author(name=f'Successful Transaction: {ctx.author}', icon_url=ctx.author.avatar.url)
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        return embed, REPLY

    @command(aliases={'u', 'consume', 'activate'})
    @simple_cooldown(2, 10)
    @user_max_concurrency(1)
    @lock_transactions
    async def use(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(USE)):
        """Use the items you own!"""
        item, quantity = item_and_quantity

        record = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.5, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            quantity = await item.use(ctx, quantity)

            if quantity > 0 and item.dispose:
                await record.inventory_manager.add_item(item, -quantity, connection=conn)

        await ctx.thumbs()

    @command(aliases={'rm', 'dispose', 'deactivate'})
    @simple_cooldown(2, 10)
    @user_max_concurrency(1)
    @lock_transactions
    async def remove(self, ctx: Context, *, item: query_item):
        """Remove the effects of active items."""
        record = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.4, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            await item.remove(ctx)

        await ctx.thumbs()

    @command(aliases={'give', 'gift', 'donate', 'pay'})
    @simple_cooldown(1, 30)
    @user_max_concurrency(1)
    @lock_transactions
    async def share(self, ctx: Context, user: CaseInsensitiveMemberConverter, *, entity: DropAmount | ItemAndQuantityConverter(DROP)):
        """Share coins or items from your inventory with another user."""
        if user.bot:
            return 'You cannot share with bots.', REPLY

        if user == ctx.author:
            return 'Sharing with yourself, that sounds kinda funny', REPLY

        if isinstance(entity, int):
            entity_human = f'{Emojis.coin} **{entity:,}**'
        else:
            item, quantity = entity
            entity_human = item.get_sentence_chunk(quantity)

        if not await ctx.confirm(
            f"Are you sure you want to give {entity_human} to {user.mention}?",
            allowed_mentions=discord.AllowedMentions.none(),
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Cancelled transaction.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        their_record = await ctx.db.get_user_record(user.id)

        async with ctx.db.acquire() as conn:
            if isinstance(entity, int):
                await record.add(wallet=-entity, connection=conn)
                await their_record.add(wallet=entity, connection=conn)

                updated = f'{Emojis.coin} **{record.wallet:,}**', f'{Emojis.coin} **{their_record.wallet:,}**'
            else:
                # noinspection PyUnboundLocalVariable
                await record.inventory_manager.add_item(item, -quantity, connection=conn)
                await their_record.inventory_manager.add_item(item, quantity, connection=conn)

                updated = (
                    f'{item.emoji} {item.name} x{record.inventory_manager.cached.quantity_of(item):,}',
                    f'{item.emoji} {item.name} x{their_record.inventory_manager.cached.quantity_of(item):,}',
                )

            await their_record.notifications_manager.add_notification(
                title='You got coins!' if isinstance(entity, int) else 'You got items!',
                content=f'{ctx.author.mention} gave you {entity_human}.',
                connection=conn,
            )

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You gave {entity_human} to {user.mention}.'
        embed.set_author(name=f'Successful Transaction: {ctx.author}', icon_url=ctx.author.avatar.url)

        us, them = updated
        embed.add_field(name='Updated Values', value=f'{ctx.author.name}: {us}\n{user.name}: {them}')

        return embed, REPLY

    @command(aliases={'giveaway'})
    @simple_cooldown(1, 4)
    @user_max_concurrency(1)
    @lock_transactions
    async def drop(self, ctx: Context, *, entity: DropAmount | ItemAndQuantityConverter(DROP)):
        """Drop coins or items from your inventory into the chat.

        The first one to click the button will retrieve your coins!
        If no one clicks the button within 120 seconds, your coins/items will be returned.
        """
        record = await ctx.db.get_user_record(ctx.author.id)

        if isinstance(entity, int):
            await record.add(wallet=-entity)
        else:
            item, quantity = entity

            inventory = await record.inventory_manager.wait()
            await inventory.add_item(item, -quantity)

        entity_type = 'coins' if isinstance(entity, int) else 'items'

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name} has dropped {entity_type}!', icon_url=ctx.author.avatar.url)

        # noinspection PyUnboundLocalVariable
        entity_human = f"{Emojis.coin} {entity:,}" if isinstance(entity, int) else item.get_sentence_chunk(quantity)
        embed.description = f'{ctx.author.mention} has dropped {entity_human}!'

        embed.set_footer(text=f'Click the button below to retrieve your {entity_type}!')

        view = DropView(ctx)
        yield embed, view, REPLY, NO_EXTRA

        embed.set_footer(text='')

        await view.wait()
        if not view.winner:
            if isinstance(entity, int):
                await record.add(wallet=entity)
            else:
                # noinspection PyUnboundLocalVariable
                await inventory.add_item(item, quantity)

            embed.description = 'No one clicked the button! Your entities have been returned.'
            embed.colour = Colors.error

            yield embed, view, EDIT
            return

        winner_record = await ctx.db.get_user_record(view.winner.id)
        if isinstance(entity, int):
            await winner_record.add(wallet=entity)
        else:
            # noinspection PyUnboundLocalVariable
            await winner_record.inventory_manager.add_item(item, quantity)

        embed.colour = Colors.success
        embed.description = f'{view.winner.mention} was the first one to click the button! They have received {entity_human}.'
        embed.set_author(name=f'Winner: {view.winner}', icon_url=view.winner.avatar.url)

        yield embed, view, EDIT

    @command(aliases={'recipe', 'rc'})
    @simple_cooldown(1, 4)
    @user_max_concurrency(1)
    async def recipes(self, ctx: Context, *, recipe: query_recipe = None):
        """View recipes and craft those you have already discovered."""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.inventory_manager.wait()

        view = RecipeView(ctx, record, default=recipe)
        yield view.build_embed(), view, REPLY

        await view.wait()

    @command(aliases={'cr', 'make'})
    @simple_cooldown(1, 10)
    @user_max_concurrency(1)
    async def craft(self, ctx: Context, *, recipe: RecipeConverter = None):
        """Craft items from your inventory to make new ones!

        If you craft an undiscovered recipe, it will be added to your discovered recipes.
        Note that you can quickly craft already discovered recipes by using the `recipes` command.
        """
        if recipe is None:
            return await ctx.invoke(self.recipes)

        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()

        if any(inventory.cached.quantity_of(item) < required for item, required in recipe.ingredients.items()):
            return "That's a valid recipe, but you don't have the required items in order to craft it.", BAD_ARGUMENT

        if record.wallet < recipe.price:
            return f"That's a valid recipe, but you don't have enough coins ({Emojis.coin} {recipe.price:,}) to craft it.", BAD_ARGUMENT

        already_discovered = recipe.key in record.discovered_recipes
        if not already_discovered:
            await record.append(discovered_recipes=recipe.key)
            message = f'{ctx.author.name} has crafted something new!'
        else:
            message = "You've already discovered this recipe!"

        async with ctx.db.acquire() as conn:
            await record.add(wallet=-recipe.price, connection=conn)

            for item, quantity in recipe.ingredients.items():
                await inventory.add_item(item, -quantity, connection=conn)

            for item, quantity in recipe.result.items():
                await inventory.add_item(item, quantity, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name=message, icon_url=ctx.author.avatar.url)
        if already_discovered:
            embed.description = f'You crafted the {recipe.emoji} **{recipe.name}** recipe, which you have already discovered.'
        else:
            embed.description = f'You have discovered the {recipe.emoji} **{recipe.name}** recipe.'

        embed.add_field(
            name='Crafted',
            value='\n'.join(f'{item.display_name} x{quantity:,}' for item, quantity in recipe.result.items()),
            inline=False
        )

        embed.add_field(
            name='Ingredients Used',
            value=f'{Emojis.coin} {recipe.price:,}\n' + '\n'.join(
                f'{item.display_name} x{quantity:,}' for item, quantity in recipe.ingredients.items()
            ),
            inline=False,
        )

        return embed, REPLY


setup = Transactions.simple_setup
