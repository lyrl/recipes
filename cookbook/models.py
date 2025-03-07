import operator
import pathlib
import re
import uuid
from datetime import date, timedelta

from annoying.fields import AutoOneToOneField
from django.contrib import auth
from django.contrib.auth.models import Group, User
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.files.uploadedfile import UploadedFile, InMemoryUploadedFile
from django.core.validators import MinLengthValidator
from django.db import models, IntegrityError
from django.db.models import Index, ProtectedError
from django.utils import timezone
from django.utils.translation import gettext as _
from treebeard.mp_tree import MP_Node, MP_NodeManager
from django_scopes import ScopedManager, scopes_disabled
from django_prometheus.models import ExportModelOperationsMixin
from recipes.settings import (COMMENT_PREF_DEFAULT, FRACTION_PREF_DEFAULT,
                              KJ_PREF_DEFAULT, STICKY_NAV_PREF_DEFAULT,
                              SORT_TREE_BY_NAME)


def get_user_name(self):
    if not (name := f"{self.first_name} {self.last_name}") == " ":
        return name
    else:
        return self.username


auth.models.User.add_to_class('get_user_name', get_user_name)


def get_model_name(model):
    return ('_'.join(re.findall('[A-Z][^A-Z]*', model.__name__))).lower()


class TreeManager(MP_NodeManager):
    # model.Manager get_or_create() is not compatible with MP_Tree
    def get_or_create(self, **kwargs):
        kwargs['name'] = kwargs['name'].strip()
        try:
            return self.get(name__exact=kwargs['name'], space=kwargs['space']), False
        except self.model.DoesNotExist:
            with scopes_disabled():
                try:
                    return self.model.add_root(**kwargs), True
                except IntegrityError as e:
                    if 'Key (path)' in e.args[0]:
                        self.model.fix_tree(fix_paths=True)
                        return self.model.add_root(**kwargs), True


class TreeModel(MP_Node):
    _full_name_separator = ' > '

    def __str__(self):
        if self.icon:
            return f"{self.icon} {self.name}"
        else:
            return f"{self.name}"

    @property
    def parent(self):
        parent = self.get_parent()
        if parent:
            return self.get_parent().id
        return None

    @property
    def full_name(self):
        """
        Returns a string representation of a tree node and it's ancestors,
        e.g. 'Cuisine > Asian > Chinese > Catonese'.
        """
        names = [node.name for node in self.get_ancestors_and_self()]
        return self._full_name_separator.join(names)

    def get_ancestors_and_self(self):
        """
        Gets ancestors and includes itself. Use treebeard's get_ancestors
        if you don't want to include the node itself. It's a separate
        function as it's commonly used in templates.
        """
        if self.is_root():
            return [self]
        return list(self.get_ancestors()) + [self]

    def get_descendants_and_self(self):
        """
        Gets descendants and includes itself. Use treebeard's get_descendants
        if you don't want to include the node itself. It's a separate
        function as it's commonly used in templates.
        """
        return self.get_tree(self)

    def has_children(self):
        return self.get_num_children() > 0

    def get_num_children(self):
        return self.get_children().count()

    # use self.objects.get_or_create() instead
    @classmethod
    def add_root(self, **kwargs):
        with scopes_disabled():
            return super().add_root(**kwargs)

    class Meta:
        abstract = True


class PermissionModelMixin:
    @staticmethod
    def get_space_key():
        return ('space',)

    def get_space_kwarg(self):
        return '__'.join(self.get_space_key())

    def get_owner(self):
        if getattr(self, 'created_by', None):
            return self.created_by
        if getattr(self, 'user', None):
            return self.user
        return None

    def get_shared(self):
        if getattr(self, 'shared', None):
            return self.shared.all()
        return []

    def get_space(self):
        p = '.'.join(self.get_space_key())
        try:
            if space := operator.attrgetter(p)(self):
                return space
        except AttributeError:
            raise NotImplementedError('get space for method not implemented and standard fields not available')


class Space(ExportModelOperationsMixin('space'), models.Model):
    name = models.CharField(max_length=128, default='Default')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.CharField(max_length=512, default='', blank=True)
    max_recipes = models.IntegerField(default=0)
    max_file_storage_mb = models.IntegerField(default=0, help_text=_('Maximum file storage for space in MB. 0 for unlimited, -1 to disable file upload.'))
    max_users = models.IntegerField(default=0)
    allow_sharing = models.BooleanField(default=True)
    demo = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class UserPreference(models.Model, PermissionModelMixin):
    # Themes
    BOOTSTRAP = 'BOOTSTRAP'
    DARKLY = 'DARKLY'
    FLATLY = 'FLATLY'
    SUPERHERO = 'SUPERHERO'
    TANDOOR = 'TANDOOR'

    THEMES = (
        (TANDOOR, 'Tandoor'),
        (BOOTSTRAP, 'Bootstrap'),
        (DARKLY, 'Darkly'),
        (FLATLY, 'Flatly'),
        (SUPERHERO, 'Superhero'),
    )

    # Nav colors
    PRIMARY = 'PRIMARY'
    SECONDARY = 'SECONDARY'
    SUCCESS = 'SUCCESS'
    INFO = 'INFO'
    WARNING = 'WARNING'
    DANGER = 'DANGER'
    LIGHT = 'LIGHT'
    DARK = 'DARK'

    COLORS = (
        (PRIMARY, 'Primary'),
        (SECONDARY, 'Secondary'),
        (SUCCESS, 'Success'),
        (INFO, 'Info'),
        (WARNING, 'Warning'),
        (DANGER, 'Danger'),
        (LIGHT, 'Light'),
        (DARK, 'Dark')
    )

    # Default Page
    SEARCH = 'SEARCH'
    PLAN = 'PLAN'
    BOOKS = 'BOOKS'

    PAGES = (
        (SEARCH, _('Search')),
        (PLAN, _('Meal-Plan')),
        (BOOKS, _('Books')),
    )

    # Search Style
    SMALL = 'SMALL'
    LARGE = 'LARGE'
    NEW = 'NEW'

    SEARCH_STYLE = ((SMALL, _('Small')), (LARGE, _('Large')), (NEW, _('New')))

    user = AutoOneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    theme = models.CharField(choices=THEMES, max_length=128, default=TANDOOR)
    nav_color = models.CharField(
        choices=COLORS, max_length=128, default=PRIMARY
    )
    default_unit = models.CharField(max_length=32, default='g')
    use_fractions = models.BooleanField(default=FRACTION_PREF_DEFAULT)
    use_kj = models.BooleanField(default=KJ_PREF_DEFAULT)
    default_page = models.CharField(
        choices=PAGES, max_length=64, default=SEARCH
    )
    search_style = models.CharField(
        choices=SEARCH_STYLE, max_length=64, default=NEW
    )
    show_recent = models.BooleanField(default=True)
    plan_share = models.ManyToManyField(
        User, blank=True, related_name='plan_share_default'
    )
    ingredient_decimals = models.IntegerField(default=2)
    comments = models.BooleanField(default=COMMENT_PREF_DEFAULT)
    shopping_auto_sync = models.IntegerField(default=5)
    sticky_navbar = models.BooleanField(default=STICKY_NAV_PREF_DEFAULT)

    created_at = models.DateTimeField(auto_now_add=True)
    space = models.ForeignKey(Space, on_delete=models.CASCADE, null=True)
    objects = ScopedManager(space='space')

    def __str__(self):
        return str(self.user)


class Storage(models.Model, PermissionModelMixin):
    DROPBOX = 'DB'
    NEXTCLOUD = 'NEXTCLOUD'
    LOCAL = 'LOCAL'
    STORAGE_TYPES = ((DROPBOX, 'Dropbox'), (NEXTCLOUD, 'Nextcloud'), (LOCAL, 'Local'))

    name = models.CharField(max_length=128)
    method = models.CharField(
        choices=STORAGE_TYPES, max_length=128, default=DROPBOX
    )
    username = models.CharField(max_length=128, blank=True, null=True)
    password = models.CharField(max_length=128, blank=True, null=True)
    token = models.CharField(max_length=512, blank=True, null=True)
    url = models.URLField(blank=True, null=True)
    path = models.CharField(blank=True, default='', max_length=256)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name


class Sync(models.Model, PermissionModelMixin):
    storage = models.ForeignKey(Storage, on_delete=models.PROTECT)
    path = models.CharField(max_length=512, default="")
    active = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.path


class SupermarketCategory(models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128, validators=[MinLengthValidator(1)])
    description = models.TextField(blank=True, null=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['space', 'name'], name='smc_unique_name_per_space')
        ]


class Supermarket(models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128, validators=[MinLengthValidator(1)])
    description = models.TextField(blank=True, null=True)
    categories = models.ManyToManyField(SupermarketCategory, through='SupermarketCategoryRelation')

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['space', 'name'], name='sm_unique_name_per_space')
        ]


class SupermarketCategoryRelation(models.Model, PermissionModelMixin):
    supermarket = models.ForeignKey(Supermarket, on_delete=models.CASCADE, related_name='category_to_supermarket')
    category = models.ForeignKey(SupermarketCategory, on_delete=models.CASCADE, related_name='category_to_supermarket')
    order = models.IntegerField(default=0)

    objects = ScopedManager(space='supermarket__space')

    @staticmethod
    def get_space_key():
        return 'supermarket', 'space'

    class Meta:
        ordering = ('order',)


class SyncLog(models.Model, PermissionModelMixin):
    sync = models.ForeignKey(Sync, on_delete=models.CASCADE)
    status = models.CharField(max_length=32)
    msg = models.TextField(default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ScopedManager(space='sync__space')

    def __str__(self):
        return f"{self.created_at}:{self.sync} - {self.status}"


class Keyword(ExportModelOperationsMixin('keyword'), TreeModel, PermissionModelMixin):
    if SORT_TREE_BY_NAME:
        node_order_by = ['name']
    name = models.CharField(max_length=64)
    icon = models.CharField(max_length=16, blank=True, null=True)
    description = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space', _manager_class=TreeManager)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['space', 'name'], name='kw_unique_name_per_space')
        ]
        indexes = (Index(fields=['id', 'name']),)


class Unit(ExportModelOperationsMixin('unit'), models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128, validators=[MinLengthValidator(1)])
    description = models.TextField(blank=True, null=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['space', 'name'], name='u_unique_name_per_space')
        ]


class Food(ExportModelOperationsMixin('food'), TreeModel, PermissionModelMixin):
    if SORT_TREE_BY_NAME:
        node_order_by = ['name']
    name = models.CharField(max_length=128, validators=[MinLengthValidator(1)])
    recipe = models.ForeignKey('Recipe', null=True, blank=True, on_delete=models.SET_NULL)
    supermarket_category = models.ForeignKey(SupermarketCategory, null=True, blank=True, on_delete=models.SET_NULL)
    ignore_shopping = models.BooleanField(default=False)
    description = models.TextField(default='', blank=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space', _manager_class=TreeManager)

    def __str__(self):
        return self.name

    def delete(self):
        if self.ingredient_set.all().exclude(step=None).count() > 0:
            raise ProtectedError(self.name + _(" is part of a recipe step and cannot be deleted"), self.ingredient_set.all().exclude(step=None))
        else:
            return super().delete()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['space', 'name'], name='f_unique_name_per_space')
        ]
        indexes = (
            Index(fields=['id']),
            Index(fields=['name']),
        )


class Ingredient(ExportModelOperationsMixin('ingredient'), models.Model, PermissionModelMixin):
    # a pre-delete signal on Food checks if the Ingredient is part of a Step, if it is raises a ProtectedError instead of cascading the delete
    food = models.ForeignKey(Food, on_delete=models.CASCADE, null=True, blank=True)
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True)
    amount = models.DecimalField(default=0, decimal_places=16, max_digits=32)
    note = models.CharField(max_length=256, null=True, blank=True)
    is_header = models.BooleanField(default=False)
    no_amount = models.BooleanField(default=False)
    order = models.IntegerField(default=0)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return str(self.amount) + ' ' + str(self.unit) + ' ' + str(self.food)

    class Meta:
        ordering = ['order', 'pk']
        indexes = (
            Index(fields=['id']),
        )


class Step(ExportModelOperationsMixin('step'), models.Model, PermissionModelMixin):
    TEXT = 'TEXT'
    TIME = 'TIME'
    FILE = 'FILE'
    RECIPE = 'RECIPE'

    name = models.CharField(max_length=128, default='', blank=True)
    type = models.CharField(
        choices=((TEXT, _('Text')), (TIME, _('Time')), (FILE, _('File')), (RECIPE, _('Recipe')),),
        default=TEXT,
        max_length=16
    )
    instruction = models.TextField(blank=True)
    ingredients = models.ManyToManyField(Ingredient, blank=True)
    time = models.IntegerField(default=0, blank=True)
    order = models.IntegerField(default=0)
    file = models.ForeignKey('UserFile', on_delete=models.PROTECT, null=True, blank=True)
    show_as_header = models.BooleanField(default=True)
    search_vector = SearchVectorField(null=True)
    step_recipe = models.ForeignKey('Recipe', default=None, blank=True, null=True, on_delete=models.PROTECT)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def get_instruction_render(self):
        from cookbook.helper.template_helper import render_instructions
        return render_instructions(self)

    def __str__(self):
        return f'{self.pk} {self.name}'

    class Meta:
        ordering = ['order', 'pk']
        indexes = (GinIndex(fields=["search_vector"]),)


class NutritionInformation(models.Model, PermissionModelMixin):
    fats = models.DecimalField(default=0, decimal_places=16, max_digits=32)
    carbohydrates = models.DecimalField(
        default=0, decimal_places=16, max_digits=32
    )
    proteins = models.DecimalField(default=0, decimal_places=16, max_digits=32)
    calories = models.DecimalField(default=0, decimal_places=16, max_digits=32)
    source = models.CharField(
        max_length=512, default="", null=True, blank=True
    )

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return f'Nutrition {self.pk}'


class Recipe(ExportModelOperationsMixin('recipe'), models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128)
    description = models.CharField(max_length=512, blank=True, null=True)
    servings = models.IntegerField(default=1)
    servings_text = models.CharField(default='', blank=True, max_length=32)
    image = models.ImageField(upload_to='recipes/', blank=True, null=True)
    storage = models.ForeignKey(
        Storage, on_delete=models.PROTECT, blank=True, null=True
    )
    file_uid = models.CharField(max_length=256, default="", blank=True)
    file_path = models.CharField(max_length=512, default="", blank=True)
    link = models.CharField(max_length=512, null=True, blank=True)
    cors_link = models.CharField(max_length=1024, null=True, blank=True)
    keywords = models.ManyToManyField(Keyword, blank=True)
    steps = models.ManyToManyField(Step, blank=True)
    working_time = models.IntegerField(default=0)
    waiting_time = models.IntegerField(default=0)
    internal = models.BooleanField(default=False)
    nutrition = models.ForeignKey(
        NutritionInformation, blank=True, null=True, on_delete=models.CASCADE
    )
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    name_search_vector = SearchVectorField(null=True)
    desc_search_vector = SearchVectorField(null=True)
    space = models.ForeignKey(Space, on_delete=models.CASCADE)

    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name

    class Meta():
        indexes = (
            GinIndex(fields=["name_search_vector"]),
            GinIndex(fields=["desc_search_vector"]),
            Index(fields=['id']),
            Index(fields=['name']),
        )


class Comment(ExportModelOperationsMixin('comment'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    text = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScopedManager(space='recipe__space')

    @staticmethod
    def get_space_key():
        return 'recipe', 'space'

    def get_space(self):
        return self.recipe.space

    def __str__(self):
        return self.text


class RecipeImport(models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128)
    storage = models.ForeignKey(Storage, on_delete=models.PROTECT)
    file_uid = models.CharField(max_length=256, default="")
    file_path = models.CharField(max_length=512, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name


class RecipeBook(ExportModelOperationsMixin('book'), models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=16, blank=True, null=True)
    shared = models.ManyToManyField(User, blank=True, related_name='shared_with')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name

    class Meta():
        indexes = (Index(fields=['name']),)


class RecipeBookEntry(ExportModelOperationsMixin('book_entry'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    book = models.ForeignKey(RecipeBook, on_delete=models.CASCADE)

    objects = ScopedManager(space='book__space')

    @staticmethod
    def get_space_key():
        return 'book', 'space'

    def __str__(self):
        return self.recipe.name

    def get_owner(self):
        try:
            return self.book.created_by
        except AttributeError:
            return None

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['recipe', 'book'], name='rbe_unique_name_per_space')
        ]


class MealType(models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128)
    order = models.IntegerField(default=0)
    icon = models.CharField(max_length=16, blank=True, null=True)
    color = models.CharField(max_length=7, blank=True, null=True)
    default = models.BooleanField(default=False, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.name


class MealPlan(ExportModelOperationsMixin('meal_plan'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, blank=True, null=True)
    servings = models.DecimalField(default=1, max_digits=8, decimal_places=4)
    title = models.CharField(max_length=64, blank=True, default='')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    shared = models.ManyToManyField(User, blank=True, related_name='plan_share')
    meal_type = models.ForeignKey(MealType, on_delete=models.CASCADE)
    note = models.TextField(blank=True)
    date = models.DateField()

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def get_label(self):
        if self.title:
            return self.title
        return str(self.recipe)

    def get_meal_name(self):
        return self.meal_type.name

    def __str__(self):
        return f'{self.get_label()} - {self.date} - {self.meal_type.name}'


class ShoppingListRecipe(ExportModelOperationsMixin('shopping_list_recipe'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, null=True, blank=True)
    servings = models.DecimalField(default=1, max_digits=8, decimal_places=4)

    objects = ScopedManager(space='recipe__space')

    @staticmethod
    def get_space_key():
        return 'recipe', 'space'

    def get_space(self):
        return self.recipe.space

    def __str__(self):
        return f'Shopping list recipe {self.id} - {self.recipe}'

    def get_owner(self):
        try:
            return self.shoppinglist_set.first().created_by
        except AttributeError:
            return None


class ShoppingListEntry(ExportModelOperationsMixin('shopping_list_entry'), models.Model, PermissionModelMixin):
    list_recipe = models.ForeignKey(ShoppingListRecipe, on_delete=models.CASCADE, null=True, blank=True)
    food = models.ForeignKey(Food, on_delete=models.CASCADE)
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, null=True, blank=True)
    amount = models.DecimalField(default=0, decimal_places=16, max_digits=32)
    order = models.IntegerField(default=0)
    checked = models.BooleanField(default=False)

    objects = ScopedManager(space='shoppinglist__space')

    @staticmethod
    def get_space_key():
        return 'shoppinglist', 'space'

    def get_space(self):
        return self.shoppinglist_set.first().space

    def __str__(self):
        return f'Shopping list entry {self.id}'

    def get_shared(self):
        return self.shoppinglist_set.first().shared.all()

    def get_owner(self):
        try:
            return self.shoppinglist_set.first().created_by
        except AttributeError:
            return None


class ShoppingList(ExportModelOperationsMixin('shopping_list'), models.Model, PermissionModelMixin):
    uuid = models.UUIDField(default=uuid.uuid4)
    note = models.TextField(blank=True, null=True)
    recipes = models.ManyToManyField(ShoppingListRecipe, blank=True)
    entries = models.ManyToManyField(ShoppingListEntry, blank=True)
    shared = models.ManyToManyField(User, blank=True, related_name='list_share')
    supermarket = models.ForeignKey(Supermarket, null=True, blank=True, on_delete=models.SET_NULL)
    finished = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return f'Shopping list {self.id}'


class ShareLink(ExportModelOperationsMixin('share_link'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    uuid = models.UUIDField(default=uuid.uuid4)
    request_count = models.IntegerField(default=0)
    abuse_blocked = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return f'{self.recipe} - {self.uuid}'


def default_valid_until():
    return date.today() + timedelta(days=14)


class InviteLink(ExportModelOperationsMixin('invite_link'), models.Model, PermissionModelMixin):
    uuid = models.UUIDField(default=uuid.uuid4)
    email = models.EmailField(blank=True)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    valid_until = models.DateField(default=default_valid_until)
    used_by = models.ForeignKey(
        User, null=True, on_delete=models.CASCADE, related_name='used_by'
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return f'{self.uuid}'


class TelegramBot(models.Model, PermissionModelMixin):
    token = models.CharField(max_length=256)
    name = models.CharField(max_length=128, default='', blank=True)
    chat_id = models.CharField(max_length=128, default='', blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    webhook_token = models.UUIDField(default=uuid.uuid4)

    objects = ScopedManager(space='space')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.name}"


class CookLog(ExportModelOperationsMixin('cook_log'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    rating = models.IntegerField(null=True)
    servings = models.IntegerField(default=0)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.recipe.name

    class Meta():
        indexes = (
            Index(fields=['id']),
            Index(fields=['recipe']),
            Index(fields=['-created_at']),
            Index(fields=['rating']),
            Index(fields=['created_by']),
            Index(fields=['created_by', 'rating']),
        )


class ViewLog(ExportModelOperationsMixin('view_log'), models.Model, PermissionModelMixin):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    objects = ScopedManager(space='space')

    def __str__(self):
        return self.recipe.name

    class Meta():
        indexes = (
            Index(fields=['recipe']),
            Index(fields=['-created_at']),
            Index(fields=['created_by']),
            Index(fields=['recipe', '-created_at', 'created_by']),
        )


class ImportLog(models.Model, PermissionModelMixin):
    type = models.CharField(max_length=32)
    running = models.BooleanField(default=True)
    msg = models.TextField(default="")
    keyword = models.ForeignKey(Keyword, null=True, blank=True, on_delete=models.SET_NULL)

    total_recipes = models.IntegerField(default=0)
    imported_recipes = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    objects = ScopedManager(space='space')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.created_at}:{self.type}"


class BookmarkletImport(ExportModelOperationsMixin('bookmarklet_import'), models.Model, PermissionModelMixin):
    html = models.TextField()
    url = models.CharField(max_length=256, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    objects = ScopedManager(space='space')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)


# field names used to configure search behavior - all data populated during data migration
# other option is to use a MultiSelectField from https://github.com/goinnn/django-multiselectfield
class SearchFields(models.Model, PermissionModelMixin):
    name = models.CharField(max_length=32, unique=True)
    field = models.CharField(max_length=64, unique=True)

    def __str__(self):
        return _(self.name)

    @staticmethod
    def get_name(self):
        return _(self.name)


def allSearchFields():
    return list(SearchFields.objects.values_list('id', flat=True))


def nameSearchField():
    return [SearchFields.objects.get(name='Name').id]


class SearchPreference(models.Model, PermissionModelMixin):
    # Search Style (validation parsleyjs.org)
    # phrase or plain or raw (websearch and trigrams are mutually exclusive)
    SIMPLE = 'plain'
    PHRASE = 'phrase'
    WEB = 'websearch'
    RAW = 'raw'
    SEARCH_STYLE = (
        (SIMPLE, _('Simple')),
        (PHRASE, _('Phrase')),
        (WEB, _('Web')),
        (RAW, _('Raw'))
    )

    user = AutoOneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    search = models.CharField(choices=SEARCH_STYLE, max_length=32, default=SIMPLE)

    lookup = models.BooleanField(default=False)
    unaccent = models.ManyToManyField(SearchFields, related_name="unaccent_fields", blank=True, default=allSearchFields)
    icontains = models.ManyToManyField(SearchFields, related_name="icontains_fields", blank=True, default=nameSearchField)
    istartswith = models.ManyToManyField(SearchFields, related_name="istartswith_fields", blank=True)
    trigram = models.ManyToManyField(SearchFields, related_name="trigram_fields", blank=True, default=nameSearchField)
    fulltext = models.ManyToManyField(SearchFields, related_name="fulltext_fields", blank=True)
    trigram_threshold = models.DecimalField(default=0.1, decimal_places=2, max_digits=3)


class UserFile(ExportModelOperationsMixin('user_files'), models.Model, PermissionModelMixin):
    name = models.CharField(max_length=128)
    file = models.FileField(upload_to='files/')
    file_size_kb = models.IntegerField(default=0, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    objects = ScopedManager(space='space')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)

    def save(self, *args, **kwargs):
        if hasattr(self.file, 'file') and isinstance(self.file.file, UploadedFile) or isinstance(self.file.file, InMemoryUploadedFile):
            self.file.name = f'{uuid.uuid4()}' + pathlib.Path(self.file.name).suffix
            self.file_size_kb = round(self.file.size / 1000)
        super(UserFile, self).save(*args, **kwargs)


class Automation(ExportModelOperationsMixin('automations'), models.Model, PermissionModelMixin):
    FOOD_ALIAS = 'FOOD_ALIAS'
    UNIT_ALIAS = 'UNIT_ALIAS'
    KEYWORD_ALIAS = 'KEYWORD_ALIAS'

    type = models.CharField(max_length=128,
                            choices=((FOOD_ALIAS, _('Food Alias')), (UNIT_ALIAS, _('Unit Alias')), (KEYWORD_ALIAS, _('Keyword Alias')),))
    name = models.CharField(max_length=128, default='')
    description = models.TextField(blank=True, null=True)

    param_1 = models.CharField(max_length=128, blank=True, null=True)
    param_2 = models.CharField(max_length=128, blank=True, null=True)
    param_3 = models.CharField(max_length=128, blank=True, null=True)

    disabled = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    objects = ScopedManager(space='space')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)
