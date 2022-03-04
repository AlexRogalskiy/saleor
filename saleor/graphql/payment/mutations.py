from typing import TYPE_CHECKING, List, Optional

import graphene
from django.core.exceptions import ValidationError

from ...channel.models import Channel
from ...checkout import models as checkout_models
from ...checkout.calculations import calculate_checkout_total_with_gift_cards
from ...checkout.checkout_cleaner import clean_billing_address, clean_checkout_shipping
from ...checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ...checkout.utils import cancel_active_payments
from ...core.error_codes import MetadataErrorCode
from ...core.permissions import OrderPermissions, PaymentPermissions
from ...core.utils import get_client_ip
from ...core.utils.url import validate_storefront_url
from ...order import models as order_models
from ...order.events import payment_event
from ...payment import PaymentError, StorePaymentMethod, gateway
from ...payment import models as payment_models
from ...payment.error_codes import PaymentCreateErrorCode, PaymentErrorCode
from ...payment.utils import create_payment, is_currency_supported
from ..account.i18n import I18nMixin
from ..channel.utils import validate_channel
from ..checkout.mutations.utils import get_checkout_by_token
from ..checkout.types import Checkout
from ..core.descriptions import ADDED_IN_31, DEPRECATED_IN_3X_INPUT
from ..core.enums import to_enum
from ..core.fields import JSONString
from ..core.mutations import BaseMutation, ModelMutation
from ..core.scalars import UUID, PositiveDecimal
from ..core.types import common as common_types
from ..core.validators import validate_one_of_args_is_in_mutation
from ..meta.mutations import MetadataInput
from .enums import PaymentActionEnum, TransactionStatusEnum
from .types import Payment, PaymentInitialized, PaymentPOC
from .utils import metadata_contains_empty_key


def description(enum):
    if enum is None:
        return "Enum representing the type of a payment storage in a gateway."
    elif enum == StorePaymentMethodEnum.NONE:
        return "Storage is disabled. The payment is not stored."
    elif enum == StorePaymentMethodEnum.ON_SESSION:
        return (
            "On session storage type. "
            "The payment is stored only to be reused when "
            "the customer is present in the checkout flow."
        )
    elif enum == StorePaymentMethodEnum.OFF_SESSION:
        return (
            "Off session storage type. "
            "The payment is stored to be reused even if the customer is absent."
        )
    return None


StorePaymentMethodEnum = to_enum(
    StorePaymentMethod, type_name="StorePaymentMethodEnum", description=description
)


class PaymentInput(graphene.InputObjectType):
    gateway = graphene.Field(
        graphene.String,
        description="A gateway to use with that payment.",
        required=True,
    )
    token = graphene.String(
        required=False,
        description=(
            "Client-side generated payment token, representing customer's "
            "billing data in a secure manner."
        ),
    )
    amount = PositiveDecimal(
        required=False,
        description=(
            "Total amount of the transaction, including "
            "all taxes and discounts. If no amount is provided, "
            "the checkout total will be used."
        ),
    )
    return_url = graphene.String(
        required=False,
        description=(
            "URL of a storefront view where user should be redirected after "
            "requiring additional actions. Payment with additional actions will not be "
            "finished if this field is not provided."
        ),
    )
    store_payment_method = StorePaymentMethodEnum(
        description=f"{ADDED_IN_31} Payment store type.",
        required=False,
        default_value=StorePaymentMethod.NONE,
    )
    metadata = graphene.List(
        graphene.NonNull(MetadataInput),
        description=f"{ADDED_IN_31} User public metadata.",
        required=False,
    )


class CheckoutPaymentCreate(BaseMutation, I18nMixin):
    checkout = graphene.Field(Checkout, description="Related checkout object.")
    payment = graphene.Field(Payment, description="A newly created payment.")

    class Arguments:
        checkout_id = graphene.ID(
            description=(
                f"The ID of the checkout. {DEPRECATED_IN_3X_INPUT} Use token instead."
            ),
            required=False,
        )
        token = UUID(description="Checkout token.", required=False)
        input = PaymentInput(
            description="Data required to create a new payment.", required=True
        )

    class Meta:
        description = "Create a new payment for given checkout."
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def clean_payment_amount(cls, info, checkout_total, amount):
        if amount != checkout_total.gross.amount:
            raise ValidationError(
                {
                    "amount": ValidationError(
                        "Partial payments are not allowed, amount should be "
                        "equal checkout's total.",
                        code=PaymentErrorCode.PARTIAL_PAYMENT_NOT_ALLOWED,
                    )
                }
            )

    @classmethod
    def validate_gateway(cls, manager, gateway_id, currency):
        """Validate if given gateway can be used for this checkout.

        Check if provided gateway_id is on the list of available payment gateways.
        Gateway will be rejected if gateway_id is invalid or a gateway doesn't support
        checkout's currency.
        """
        if not is_currency_supported(currency, gateway_id, manager):
            raise ValidationError(
                {
                    "gateway": ValidationError(
                        f"The gateway {gateway_id} is not available for this checkout.",
                        code=PaymentErrorCode.NOT_SUPPORTED_GATEWAY.value,
                    )
                }
            )

    @classmethod
    def validate_token(cls, manager, gateway: str, input_data: dict, channel_slug: str):
        token = input_data.get("token")
        is_required = manager.token_is_required_as_payment_input(gateway, channel_slug)
        if not token and is_required:
            raise ValidationError(
                {
                    "token": ValidationError(
                        f"Token is required for {gateway}.",
                        code=PaymentErrorCode.REQUIRED.value,
                    ),
                }
            )

    @classmethod
    def validate_return_url(cls, input_data):
        return_url = input_data.get("return_url")
        if not return_url:
            return
        try:
            validate_storefront_url(return_url)
        except ValidationError as error:
            raise ValidationError(
                {"redirect_url": error}, code=PaymentErrorCode.INVALID
            )

    @classmethod
    def validate_metadata_keys(cls, metadata_list: List[dict]):
        if metadata_contains_empty_key(metadata_list):
            raise ValidationError(
                {
                    "input": ValidationError(
                        {
                            "metadata": ValidationError(
                                "Metadata key cannot be empty.",
                                code=MetadataErrorCode.REQUIRED.value,
                            )
                        }
                    )
                }
            )

    @staticmethod
    def validate_checkout_email(checkout: "checkout_models.Checkout"):
        if not checkout.email:
            raise ValidationError(
                "Checkout email must be set.",
                code=PaymentErrorCode.CHECKOUT_EMAIL_NOT_SET.value,
            )

    @classmethod
    def perform_mutation(cls, _root, info, checkout_id=None, token=None, **data):
        # DEPRECATED
        validate_one_of_args_is_in_mutation(
            PaymentErrorCode, "checkout_id", checkout_id, "token", token
        )

        if token:
            checkout = get_checkout_by_token(token)
        # DEPRECATED
        else:
            checkout = cls.get_node_or_error(
                info, checkout_id or token, only_type=Checkout, field="checkout_id"
            )

        cls.validate_checkout_email(checkout)

        data = data["input"]
        gateway = data["gateway"]

        manager = info.context.plugins
        cls.validate_gateway(manager, gateway, checkout.currency)
        cls.validate_return_url(data)

        lines, unavailable_variant_pks = fetch_checkout_lines(checkout)
        if unavailable_variant_pks:
            not_available_variants_ids = {
                graphene.Node.to_global_id("ProductVariant", pk)
                for pk in unavailable_variant_pks
            }
            raise ValidationError(
                {
                    "token": ValidationError(
                        "Some of the checkout lines variants are unavailable.",
                        code=PaymentErrorCode.UNAVAILABLE_VARIANT_IN_CHANNEL.value,
                        params={"variants": not_available_variants_ids},
                    )
                }
            )
        if not lines:
            raise ValidationError(
                {
                    "lines": ValidationError(
                        "Cannot create payment for checkout without lines.",
                        code=PaymentErrorCode.NO_CHECKOUT_LINES.value,
                    )
                }
            )
        checkout_info = fetch_checkout_info(
            checkout, lines, info.context.discounts, manager
        )

        cls.validate_token(
            manager, gateway, data, channel_slug=checkout_info.channel.slug
        )

        address = (
            checkout.shipping_address or checkout.billing_address
        )  # FIXME: check which address we need here
        checkout_total = calculate_checkout_total_with_gift_cards(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            address=address,
            discounts=info.context.discounts,
        )
        amount = data.get("amount", checkout_total.gross.amount)
        clean_checkout_shipping(checkout_info, lines, PaymentErrorCode)
        clean_billing_address(checkout_info, PaymentErrorCode)
        cls.clean_payment_amount(info, checkout_total, amount)
        extra_data = {
            "customer_user_agent": info.context.META.get("HTTP_USER_AGENT"),
        }

        cancel_active_payments(checkout)

        metadata = data.get("metadata")

        if metadata is not None:
            cls.validate_metadata_keys(metadata)
            metadata = {data.key: data.value for data in metadata}

        payment = None
        if amount != 0:
            payment = create_payment(
                gateway=gateway,
                payment_token=data.get("token", ""),
                total=amount,
                currency=checkout.currency,
                email=checkout.get_customer_email(),
                extra_data=extra_data,
                # FIXME this is not a customer IP address. It is a client storefront ip
                customer_ip_address=get_client_ip(info.context),
                checkout=checkout,
                return_url=data.get("return_url"),
                store_payment_method=data["store_payment_method"],
                metadata=metadata,
            )

        return CheckoutPaymentCreate(payment=payment, checkout=checkout)


class PaymentCapture(BaseMutation):
    payment = graphene.Field(Payment, description="Updated payment.")

    class Arguments:
        payment_id = graphene.ID(required=True, description="Payment ID.")
        amount = PositiveDecimal(description="Transaction amount.")

    class Meta:
        description = "Captures the authorized payment amount."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def perform_mutation(cls, _root, info, payment_id, amount=None):
        payment = cls.get_node_or_error(
            info, payment_id, field="payment_id", only_type=Payment
        )
        channel_slug = (
            payment.order.channel.slug
            if payment.order
            else payment.checkout.channel.slug
        )
        try:
            gateway.capture(
                payment, info.context.plugins, amount=amount, channel_slug=channel_slug
            )
            payment.refresh_from_db()
        except PaymentError as e:
            raise ValidationError(str(e), code=PaymentErrorCode.PAYMENT_ERROR)
        return PaymentCapture(payment=payment)


class PaymentRefund(PaymentCapture):
    class Meta:
        description = "Refunds the captured payment amount."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def perform_mutation(cls, _root, info, payment_id, amount=None):
        payment = cls.get_node_or_error(
            info, payment_id, field="payment_id", only_type=Payment
        )
        channel_slug = (
            payment.order.channel.slug
            if payment.order
            else payment.checkout.channel.slug
        )
        try:
            gateway.refund(
                payment, info.context.plugins, amount=amount, channel_slug=channel_slug
            )
            payment.refresh_from_db()
        except PaymentError as e:
            raise ValidationError(str(e), code=PaymentErrorCode.PAYMENT_ERROR)
        return PaymentRefund(payment=payment)


class PaymentVoid(BaseMutation):
    payment = graphene.Field(Payment, description="Updated payment.")

    class Arguments:
        payment_id = graphene.ID(required=True, description="Payment ID.")

    class Meta:
        description = "Voids the authorized payment."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def perform_mutation(cls, _root, info, payment_id):
        payment = cls.get_node_or_error(
            info, payment_id, field="payment_id", only_type=Payment
        )
        channel_slug = (
            payment.order.channel.slug
            if payment.order
            else payment.checkout.channel.slug
        )
        try:
            gateway.void(payment, info.context.plugins, channel_slug=channel_slug)
            payment.refresh_from_db()
        except PaymentError as e:
            raise ValidationError(str(e), code=PaymentErrorCode.PAYMENT_ERROR)
        return PaymentVoid(payment=payment)


class PaymentInitialize(BaseMutation):
    initialized_payment = graphene.Field(PaymentInitialized, required=False)

    class Arguments:
        gateway = graphene.String(
            description="A gateway name used to initialize the payment.",
            required=True,
        )
        channel = graphene.String(
            description="Slug of a channel for which the data should be returned.",
        )
        payment_data = JSONString(
            required=False,
            description=(
                "Client-side generated data required to initialize the payment."
            ),
        )

    class Meta:
        description = "Initializes payment process when it is required by gateway."
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def validate_channel(cls, channel_slug):
        try:
            channel = Channel.objects.get(slug=channel_slug)
        except Channel.DoesNotExist:
            raise ValidationError(
                {
                    "channel": ValidationError(
                        f"Channel with '{channel_slug}' slug does not exist.",
                        code=PaymentErrorCode.NOT_FOUND.value,
                    )
                }
            )
        if not channel.is_active:
            raise ValidationError(
                {
                    "channel": ValidationError(
                        f"Channel with '{channel_slug}' is inactive.",
                        code=PaymentErrorCode.CHANNEL_INACTIVE.value,
                    )
                }
            )
        return channel

    @classmethod
    def perform_mutation(cls, _root, info, gateway, channel, payment_data):
        cls.validate_channel(channel_slug=channel)

        try:
            response = info.context.plugins.initialize_payment(
                gateway, payment_data, channel_slug=channel
            )
        except PaymentError as e:
            raise ValidationError(
                {
                    "payment_data": ValidationError(
                        str(e), code=PaymentErrorCode.INVALID.value
                    )
                }
            )
        return PaymentInitialize(initialized_payment=response)


class MoneyInput(graphene.InputObjectType):
    currency = graphene.String(description="Currency code.", required=True)
    amount = PositiveDecimal(description="Amount of money.", required=True)


class CardInput(graphene.InputObjectType):
    code = graphene.String(
        description=(
            "Payment method nonce, a token returned "
            "by the appropriate provider's SDK."
        ),
        required=True,
    )
    cvc = graphene.String(description="Card security code.", required=False)
    money = MoneyInput(
        description="Information about currency and amount.", required=True
    )


class PaymentCheckBalanceInput(graphene.InputObjectType):
    gateway_id = graphene.types.String(
        description="An ID of a payment gateway to check.", required=True
    )
    method = graphene.types.String(description="Payment method name.", required=True)
    channel = graphene.String(
        description="Slug of a channel for which the data should be returned.",
        required=True,
    )
    card = CardInput(description="Information about card.", required=True)


class PaymentCheckBalance(BaseMutation):
    data = JSONString(description="Response from the gateway.")

    class Arguments:
        input = PaymentCheckBalanceInput(
            description="Fields required to check payment balance.", required=True
        )

    class Meta:
        description = "Check payment balance."
        error_type_class = common_types.PaymentError
        error_type_field = "payment_errors"

    @classmethod
    def perform_mutation(cls, _root, info, **data):
        manager = info.context.plugins
        gateway_id = data["input"]["gateway_id"]
        money = data["input"]["card"].get("money", {})

        cls.validate_gateway(gateway_id, manager)
        cls.validate_currency(money.currency, gateway_id, manager)

        channel = data["input"].pop("channel")
        validate_channel(channel, PaymentErrorCode)

        try:
            data = manager.check_payment_balance(data["input"], channel)
        except PaymentError as e:
            raise ValidationError(
                str(e), code=PaymentErrorCode.BALANCE_CHECK_ERROR.value
            )

        return PaymentCheckBalance(data=data)

    @classmethod
    def validate_gateway(cls, gateway_id, manager):
        gateways_id = [gateway.id for gateway in manager.list_payment_gateways()]

        if gateway_id not in gateways_id:
            raise ValidationError(
                {
                    "gateway_id": ValidationError(
                        f"The gateway_id {gateway_id} is not available.",
                        code=PaymentErrorCode.NOT_SUPPORTED_GATEWAY.value,
                    )
                }
            )

    @classmethod
    def validate_currency(cls, currency, gateway_id, manager):
        if not is_currency_supported(currency, gateway_id, manager):
            raise ValidationError(
                {
                    "currency": ValidationError(
                        f"The currency {currency} is not available for {gateway_id}.",
                        code=PaymentErrorCode.NOT_SUPPORTED_GATEWAY.value,
                    )
                }
            )


class PaymentPOCCommonInput(graphene.InputObjectType):
    status = graphene.String(
        description="Status of the payment.",
    )
    type = graphene.String(
        description="Payment type used for this payment.",
    )
    reference = graphene.String(description="Reference of the payment.")
    available_actions = graphene.List(
        graphene.NonNull(PaymentActionEnum),
        description="List of all possible actions for the payment",
    )
    amount_authorized = MoneyInput(description="Amount authorized by this payment.")
    amount_captured = MoneyInput(description="Amount captured by this payment.")
    amount_refunded = MoneyInput(description="Amount refunded by this payment.")
    amount_voided = MoneyInput(description="Amount refunded by this payment.")

    public_metadata = graphene.List(
        graphene.NonNull(MetadataInput),
        description=f"{ADDED_IN_31} User public metadata.",
        required=False,
    )
    private_metadata = graphene.List(
        graphene.NonNull(MetadataInput),
        description=f"{ADDED_IN_31} User public metadata.",
        required=False,
    )


class PaymentPOCUpdateInput(PaymentPOCCommonInput):
    ...


class PaymentPOCCreateInput(PaymentPOCCommonInput):
    status = graphene.String(description="Status of the payment.", required=True)
    type = graphene.String(
        description="Payment type used for this payment.", required=True
    )


class TransactionInput(graphene.InputObjectType):
    status = graphene.Field(
        TransactionStatusEnum,
        required=True,
        description="Current status of the payment transaction.",
    )
    reference = graphene.String(description="Reference of the transaction.")
    name = graphene.String(description="Name of the transaction.")


class PaymentCreate(BaseMutation):
    payment = graphene.Field(PaymentPOC, required=True)

    class Arguments:
        id = graphene.ID(
            description=f"The ID of the checkout or order.",
            required=True,
        )
        payment = PaymentPOCCreateInput(
            required=True,
            description="Input data required to create a new payment object.",
        )
        transaction = TransactionInput(
            description="Data that defines a payment transaction."
        )

    class Meta:
        description = "Create payment for checkout or order."
        error_type_class = common_types.PaymentCreateError
        permissions = (PaymentPermissions.HANDLE_PAYMENTS,)

    @classmethod
    def perform_mutation(cls, root, info, **data):
        instance_id = data.get("id")
        instance = cls.get_node_or_error(info, instance_id)

        if not isinstance(instance, (checkout_models.Checkout, order_models.Order)):
            raise ValidationError(
                {
                    "id": ValidationError(
                        f"Couldn't resolve to Checkout or Order: {instance_id}",
                        code=PaymentCreateErrorCode.NOT_FOUND.value,
                    )
                }
            )

        # FIXME all amounts recieved in input should have a validation of currency and
        #  compared to order's currency

        # FIXME we need to have a validation here.
        payment_data = {**data["payment"]}
        payment_data["currency"] = instance.currency
        if isinstance(instance, checkout_models.Checkout):
            payment_data["checkout_id"] = instance.pk
        else:
            payment_data["order_id"] = instance.pk
            if transaction_data := data.get("transaction"):
                payment_event(
                    order=instance,
                    user=info.context.user,
                    app=info.context.app,
                    reference=transaction_data.get("reference", ""),
                    status=transaction_data.get("status", ""),
                    name=transaction_data.get("name", ""),
                )
        payment = payment_models.PaymentPOC.objects.create(**payment_data)
        return PaymentCreate(payment=payment)


class PaymentUpdate(BaseMutation):
    payment = graphene.Field(PaymentPOC, required=True)

    class Arguments:
        id = graphene.ID(
            description=f"The ID of the payment.",
            required=True,
        )
        payment = PaymentPOCUpdateInput(
            required=True,
            description="Input data required to create a new payment object.",
        )
        transaction = TransactionInput(
            description="Data that defines a payment transaction."
        )

    class Meta:
        description = "Create payment for checkout or order."
        error_type_class = common_types.PaymentCreateError
        permissions = (PaymentPermissions.HANDLE_PAYMENTS,)
        model = payment_models.PaymentPOC
        object_type = PaymentPOC

    @classmethod
    def construct_instance(cls, instance, cleaned_data):
        instance = super().construct_instance(instance, cleaned_data)
        if amount_authorized := cleaned_data.get("amount_authorized"):
            instance.authorized_value = amount_authorized.get("amount")
        if amount_captured := cleaned_data.get("amount_captured"):
            instance.captured_value = amount_captured.get("amount")
        if amount_refunded := cleaned_data.get("amount_refunded"):
            instance.refunded_value = amount_refunded.get("amount")
        if amount_voided := cleaned_data.get("amount_voided"):
            instance.voided_value = amount_voided.get("amount")
        return instance

    @classmethod
    def perform_mutation(cls, root, info, **data):
        instance_id = data.get("id")
        instance = cls.get_node_or_error(info, instance_id, only_type=PaymentPOC)
        instance = cls.construct_instance(instance, data.get("payment"))
        instance.save()
        transaction_data = data.get("transaction")
        if instance.order_id and transaction_data:
            payment_event(
                order=instance,
                user=info.context.user,
                app=info.context.app,
                reference=transaction_data.get("reference", ""),
                status=transaction_data.get("status", ""),
                name=transaction_data.get("name", ""),
            )
        return PaymentUpdate(payment=instance)
