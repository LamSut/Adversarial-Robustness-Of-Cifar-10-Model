import math
import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
from absl import app, flags
from easydict import EasyDict
from keras import Model, metrics
from keras.layers import AveragePooling2D, Conv2D
import matplotlib.pyplot as plt

from cleverhans.tf2.attacks.projected_gradient_descent import projected_gradient_descent
from cleverhans.tf2.attacks.fast_gradient_method import fast_gradient_method

FLAGS = flags.FLAGS

class CNN(Model):
    def __init__(self, nb_filters=64):
        super(CNN, self).__init__()
        img_size = 32
        log_resolution = int(round(math.log(img_size) / math.log(2)))
        conv_args = dict(activation=tf.nn.leaky_relu, kernel_size=3, padding="same")
        self.layers_obj = []
        for scale in range(log_resolution - 2):
            conv1 = Conv2D(nb_filters << scale, **conv_args)
            conv2 = Conv2D(nb_filters << (scale + 1), **conv_args)
            pool = AveragePooling2D(pool_size=(2, 2), strides=(2, 2))
            self.layers_obj.append(conv1)
            self.layers_obj.append(conv2)
            self.layers_obj.append(pool)
        conv = Conv2D(10, **conv_args)
        self.layers_obj.append(conv)

    def call(self, x):
        for layer in self.layers_obj:
            x = layer(x)
        return tf.reduce_mean(x, [1, 2])


def ld_cifar10():
    """Load training and test data."""

    def convert_types(image, label):
        image = tf.cast(image, tf.float32)
        image /= 127.5
        image -= 1.0
        return image, label

    dataset, info = tfds.load("cifar10", with_info=True, as_supervised=True)

    def augment_mirror(x):
        return tf.image.random_flip_left_right(x)

    def augment_shift(x, w=4):
        y = tf.pad(x, [[w] * 2, [w] * 2, [0] * 2], mode="REFLECT")
        return tf.image.random_crop(y, tf.shape(x))

    cifar10_train, cifar10_test = dataset["train"], dataset["test"]
    # Augmentation helps a lot in CIFAR10
    cifar10_train = cifar10_train.map(
        lambda x, y: (augment_mirror(augment_shift(x)), y)
    )
    cifar10_train = cifar10_train.map(convert_types).shuffle(10000).batch(128)
    cifar10_test = cifar10_test.map(convert_types).batch(128)

    return EasyDict(train=cifar10_train, test=cifar10_test)


def main(_):
    # Load training and test data
    data = ld_cifar10()
    model = CNN()
    loss_object = tf.losses.SparseCategoricalCrossentropy(from_logits=True)
    optimizer = tf.optimizers.Adam(learning_rate=0.001)
    
    # Metrics to track the different accuracies.
    train_loss = tf.metrics.Mean(name="train_loss")
    test_acc_clean = tf.metrics.SparseCategoricalAccuracy()
    test_acc_fgsm = tf.metrics.SparseCategoricalAccuracy()
    test_acc_pgd = tf.metrics.SparseCategoricalAccuracy()
    
    # Metrics to track the different accuracies on graph plotted.
    test_acc_clean_graph = metrics.SparseCategoricalAccuracy(name='test_acc_clean')
    test_acc_fgsm_graph = metrics.SparseCategoricalAccuracy(name='test_acc_fgsm')
    test_acc_pgd_graph = metrics.SparseCategoricalAccuracy(name='test_acc_pgd')
    
    # Initialize empty lists to store history:
    test_acc_clean_history = []
    test_acc_fgsm_history = []
    test_acc_pgd_history = []

    @tf.function
    def train_step(x, y):
        with tf.GradientTape() as tape:
            predictions = model(x)
            loss = loss_object(y, predictions)
        gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))
        train_loss(loss)


    # Train model with adversarial training
    for epoch in range(FLAGS.nb_epochs):
        # keras like display of progress
        progress_bar_train = tf.keras.utils.Progbar(50000)
        for (x, y) in data.train:
            if FLAGS.adv_train:
                # Replace clean example with adversarial example for adversarial training
                x = projected_gradient_descent(model, x, FLAGS.eps, 0.01, 40, np.inf)
            train_step(x, y)
            progress_bar_train.add(x.shape[0], values=[("loss", train_loss.result())])

    # Evaluate on clean and adversarial data
    progress_bar_test = tf.keras.utils.Progbar(10000)
    for x, y in data.test:
        y_pred = model(x)
        test_acc_clean(y, y_pred)
        test_acc_clean_graph.update_state(y, y_pred)
        
        x_fgm = fast_gradient_method(model, x, FLAGS.eps, np.inf)
        y_pred_fgm = model(x_fgm)
        test_acc_fgsm(y, y_pred_fgm)
        test_acc_fgsm_graph.update_state(y, y_pred_fgm)

        x_pgd = projected_gradient_descent(model, x, FLAGS.eps, 0.01, 40, np.inf)
        y_pred_pgd = model(x_pgd)
        test_acc_pgd(y, y_pred_pgd)
        test_acc_pgd_graph.update_state(y, y_pred_pgd)
        
        progress_bar_test.add(x.shape[0])
        
        # Append metrics to history lists after each test batch
        test_acc_clean_history.append(test_acc_clean_graph.result())
        test_acc_fgsm_history.append(test_acc_fgsm_graph.result())
        test_acc_pgd_history.append(test_acc_pgd_graph.result())

        # Reset metrics for the next test batch
        test_acc_clean_graph.reset_states()
        test_acc_fgsm_graph.reset_states()
        test_acc_pgd_graph.reset_states()

    print(
        "test acc on clean examples (%): {:.3f}".format(test_acc_clean.result() * 100)
    )
    print(
        "test acc on FGM adversarial examples (%): {:.3f}".format(
            test_acc_fgsm.result() * 100
        )
    )
    print(
        "test acc on PGD adversarial examples (%): {:.3f}".format(
            test_acc_pgd.result() * 100
        )
    )

    # Example plot after each epoch:
    plt.figure(figsize=(9, 6))

    plt.subplot(1, 1, 1)
    plt.plot(test_acc_clean_history, label='Clean Acc')
    plt.plot(test_acc_fgsm_history, label='FGM Acc')
    plt.plot(test_acc_pgd_history, label='PGD Acc')
    plt.xlabel('Epoch')
    plt.ylabel('Accuracy')
    plt.title('Test Accuracy History (Clean, FGM, PGD)')
    plt.legend()

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    flags.DEFINE_integer("nb_epochs", 8, "Number of epochs.")
    flags.DEFINE_float("eps", 0.05, "Total epsilon for FGM and PGD attacks.")
    flags.DEFINE_bool(
        "adv_train", True, "Use adversarial training (on PGD adversarial examples)."
    )
    app.run(main)
