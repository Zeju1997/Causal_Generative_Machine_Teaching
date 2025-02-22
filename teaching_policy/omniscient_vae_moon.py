from __future__ import absolute_import, division, print_function

import numpy as np
import time
import json

import sys

import torch
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter
import torch.nn as nn
import json
from tqdm import tqdm
import torchvision
import torchvision.utils as vutils
from torchvision.utils import save_image, make_grid
from train_utils import *
import teachers.omniscient_teacher as omniscient
import teachers.utils as utils
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import cv2

from datasets import BaseDataset

from experiments import SGDTrainer, IMTTrainer, WSTARTrainer

import networks.cgan as cgan
import networks.unrolled_vae as unrolled

from sklearn.datasets import make_moons, make_classification
from sklearn.model_selection import train_test_split

from utils.visualize import make_results_video, make_results_video_2d, make_results_img, make_results_img_2d, plot_generated_samples_2d, plot_classifier, plot_distribution
from utils.data import init_data, plot_graphs, load_experiment_result
from utils.network import initialize_weights

from vaes.models import VAE_HalfMoon

import subprocess
import glob

import csv

sys.path.append('..') #Hack add ROOT DIR
from baseconfig import CONF



class Trainer:
    def __init__(self, options):
        self.opt = options

        self.opt.model_name = "omniscient_vae_" + self.opt.data_mode

        self.opt.log_path = os.path.join(CONF.PATH.LOG, self.opt.model_name)
        if not os.path.exists(self.opt.log_path):
            os.makedirs(self.opt.log_path)

        self.visualize = False

        self.models = {}
        self.parameters_to_train = []

        self.device = torch.device("cpu" if self.opt.no_cuda else "cuda")

        self.get_teacher_student()

        self.pre_train = False

        self.writers = {}
        for mode in ["train", "val"]:
            self.writers[mode] = SummaryWriter(os.path.join(self.opt.log_path, mode))

    def get_teacher_student(self):
        if self.opt.data_mode == "cifar10":
            self.teacher = omniscient.OmniscientConvTeacher(self.opt.eta)
            self.student = omniscient.OmniscientConvStudent(self.opt.eta)
        else: # mnist / gaussian / moon
            self.teacher = omniscient.OmniscientLinearTeacher(self.opt.dim)
            self.teacher.apply(initialize_weights)
            torch.save(self.teacher.state_dict(), 'teacher_w0.pth')
            # self.teacher.load_state_dict(torch.load('teacher_w0.pth'))

            self.student = omniscient.OmniscientLinearStudent(self.opt.dim)
            self.baseline = omniscient.OmniscientLinearStudent(self.opt.dim)

            # self.teacher = omniscient.TeacherClassifier(self.opt.dim)
            # self.student = omniscient.StudentClassifier(self.opt.dim)

    def set_train(self):
        """Convert all models to training mode
        """
        for m in self.models.values():
            m.train()

    def set_eval(self):
        """Convert all models to testing/evaluation mode
        """
        for m in self.models.values():
            m.eval()

    def init_data(self, dim, nb_data_per_class):
        """
        Création des données gaussien
        :param dim: la dimension des données
        :param nb_data_per_class: le nombre d'exemple par classe
        :return: un tuple (données, labels)
        """
        X1 = np.random.multivariate_normal([0.5] * dim, np.identity(dim), nb_data_per_class)
        y1 = np.ones((nb_data_per_class,))

        X2 = np.random.multivariate_normal([-0.5] * dim, np.identity(dim), nb_data_per_class)
        y2 = np.zeros((nb_data_per_class,))

        X = np.concatenate((X1, X2), axis=0)
        y = np.concatenate((y1, y2), axis=0)

        indices = np.indices((nb_data_per_class * 2,))
        np.random.shuffle(indices)

        X = X[indices]
        y = y[indices]
        return X.squeeze(0), y.squeeze(0)

    def sample_image(self, net_G, n_row, batches_done):
        """Saves a grid of generated digits ranging from 0 to n_classes"""
        # Sample noise
        z = Variable(torch.cuda.FloatTensor(np.random.normal(0, 1, (n_row ** 2, self.opt.latent_dim))))
        # Get labels ranging from 0 to n_classes for n rows
        labels = np.array([num for _ in range(n_row) for num in range(n_row)])
        labels = Variable(torch.cuda.LongTensor(labels))
        gen_imgs = net_G(z, labels)
        save_image(gen_imgs.data, "images/%d.png" % batches_done, nrow=n_row, normalize=True)

    def data_sampler(self, X, Y, i):
        i_min = i * self.opt.batch_size
        i_max = (i + 1) * self.opt.batch_size

        x = X[i_min:i_max].to(self.device)
        y = Y[i_min:i_max].to(self.device)

        return x, y

    def main(self):
        """Run the random teacher (SGD), baseline (IMT) and the DHT sequentially
        """

        print("Training")
        # self.set_train()

        if self.opt.init_data:
            init_data(self.opt)

        X = torch.load('X.pt')
        Y = torch.load('Y.pt')

        nb_batch = int(self.opt.nb_train / self.opt.batch_size)

        if self.opt.data_mode == "cifar10":
            X_train = torch.tensor(X[:self.opt.nb_train])
            Y_train = torch.tensor(Y[:self.opt.nb_train], dtype=torch.long)
            X_test = torch.tensor(X[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test])
            Y_test = torch.tensor(Y[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test], dtype=torch.long)

        elif self.opt.data_mode == "mnist":
            X_train = torch.tensor(X[:self.opt.nb_train], dtype=torch.float)
            Y_train = torch.tensor(Y[:self.opt.nb_train], dtype=torch.float)
            X_test = torch.tensor(X[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test], dtype=torch.float)
            Y_test = torch.tensor(Y[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test], dtype=torch.float)

            data_train = BaseDataset(X_train, Y_train)
            train_loader = DataLoader(data_train, batch_size=self.opt.batch_size, drop_last=True, shuffle=True)

            X_train = X_train.reshape((self.opt.nb_train, self.opt.img_size**2))
            X_test = X_test.reshape((self.opt.nb_test, self.opt.img_size**2))

            img_shape = (self.opt.channels, self.opt.img_size, self.opt.img_size)
            proj_matrix = torch.empty(int(np.prod(img_shape)), self.opt.dim).normal_(mean=0, std=0.1)
            X_train = X_train.float() @ proj_matrix
            X_test = X_test.float() @ proj_matrix

        else:
            X_train = torch.tensor(X[:self.opt.nb_train], dtype=torch.float)
            Y_train = torch.tensor(Y[:self.opt.nb_train], dtype=torch.float)
            X_test = torch.tensor(X[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test], dtype=torch.float)
            Y_test = torch.tensor(Y[self.opt.nb_train:self.opt.nb_train + self.opt.nb_test], dtype=torch.float)

            data_train = BaseDataset(X_train, Y_train)
            train_loader = DataLoader(data_train, batch_size=self.opt.batch_size, drop_last=True, shuffle=True)

        # data_train = BaseDataset(X_train, Y_train)
        # data_test = BaseDataset(X_test, Y_test)
        # train_loader = DataLoader(data_train, batch_size=self.opt.batch_size, drop_last=True)
        # test_loader = DataLoader(data_test, batch_size=self.opt.batch_size, drop_last=True)

        # ---------------------
        #  Train Teacher
        # ---------------------

        if self.opt.train_wstar == True:
            wstar_trainer = WSTARTrainer(self.opt, X_train, Y_train, X_test, Y_test)
            wstar_trainer.train(self.teacher)

        self.teacher.load_state_dict(torch.load('teacher_wstar.pth'))
        w_star = self.teacher.lin.weight
        w_star = w_star / torch.norm(w_star)

        # ---------------------
        #  Train SGD
        # ---------------------

        self.opt.experiment = "SGD"
        if self.opt.train_sgd == False:

            sgd_example = utils.BaseLinear(self.opt.dim)
            sgd_example.load_state_dict(torch.load('teacher_w0.pth'))

            sgd_trainer = SGDTrainer(self.opt, X_train, Y_train, X_test, Y_test)
            _, _ = sgd_trainer.train(sgd_example, w_star)

        res_sgd, w_diff_sgd = load_experiment_result(self.opt)

        # ---------------------
        #  Train IMT Baseline
        # ---------------------

        self.opt.experiment = "IMT_Baseline"
        if self.opt.train_baseline == False:
            self.baseline.load_state_dict(torch.load('teacher_w0.pth'))

            imt_trainer = IMTTrainer(self.opt, X_train, Y_train, X_test, Y_test)
            _, _ = imt_trainer.train(self.baseline, self.teacher, w_star)

        res_baseline, w_diff_baseline = load_experiment_result(self.opt)

        # ---------------------
        #  Train Student
        # ---------------------
        if self.pre_train:
            vae = VAE_HalfMoon(self.device)
            vae = vae.to(self.device)

            optimizer = torch.optim.Adam(params=vae.parameters(), lr=0.001, weight_decay=1e-5)

            # set to training mode
            vae.train()

            train_loss_avg = []

            print('Training VAE ...')
            n_epochs = 600
            for epoch in range(n_epochs):
                train_loss_avg.append(0)
                num_batches = 0

                for x_batch, y_batch in train_loader:
                    optimizer.zero_grad()

                    y_batch = F.one_hot(y_batch.long(), num_classes=2).type(torch.FloatTensor) * 2. - 1
                    y_batch = y_batch.to(self.device)

                    x_batch = x_batch.to(self.device)

                    loss, _ = vae(x_batch, y_batch)

                    # backpropagation
                    loss.backward()

                    # one step of the optmizer (using the gradients from backpropagation)
                    optimizer.step()

                    train_loss_avg[-1] += loss.item()
                    num_batches += 1

                train_loss_avg[-1] /= num_batches
                print('Epoch [%d / %d] average negative ELBO: %f' % (epoch+1, n_epochs, train_loss_avg[-1]))

            torch.save(vae.state_dict(), 'pretrained_vae.pth')

            vae.load_state_dict(torch.load('pretrained_vae.pth'))
            vae.eval()
            with torch.no_grad():
                X, y_logits = vae.sample(num=1000)

            X = X.data.cpu().numpy()
            y = torch.argmax(y_logits, dim=1).data.cpu().numpy()

            cm = plt.cm.RdBu
            cm_bright = ListedColormap(['#FF0000', '#0000FF'])

            fig, ax = plt.subplots()
            ax.set_title("Input data")

            ax.scatter(X[:, 0], X[:, 1], c=y, cmap=cm_bright, edgecolors='k')

            plt.tight_layout()
            plt.show()

        if self.opt.train_student == True:
            self.opt.experiment = "Student"

            print("Start training {} ...".format(self.opt.experiment))
            logname = os.path.join(self.opt.log_path, 'results' + '_' + self.opt.experiment + '_' + str(self.opt.seed) + '.csv')
            if not os.path.exists(logname):
                with open(logname, 'w') as logfile:
                    logwriter = csv.writer(logfile, delimiter=',')
                    logwriter.writerow(['iter', 'test acc', 'w diff'])

            tmp_student = utils.BaseLinear(self.opt.dim)

            if self.opt.data_mode == "mnist":
                netG = unrolled.Generator(self.opt, self.teacher, tmp_student).to(self.device)
                vae = VAE_HalfMoon(self.device).to(self.device)
                unrolled_optimizer = unrolled.UnrolledOptimizer(opt=self.opt, teacher=self.teacher, student=tmp_student, generator=netG, vae=vae, X=X_train.to(self.device), Y=Y_train.to(self.device), proj_matrix=proj_matrix)
            else:
                netG = unrolled.Generator_moon(self.opt, self.teacher, tmp_student).to(self.device)
                vae = VAE_HalfMoon(self.device).to(self.device)
                unrolled_optimizer = unrolled.UnrolledOptimizer_moon(opt=self.opt, teacher=self.teacher, student=tmp_student, generator=netG, vae=vae, X=X_train.to(self.device), Y=Y_train.to(self.device))

            netG.apply(initialize_weights)
            optimG = torch.optim.Adam(netG.parameters(), lr=0.0002, betas=(0.5, 0.999))

            self.step = 0
            loss_student = []
            img_shape = (1, 28, 28)
            w_init = self.student.lin.weight

            for epoch in tqdm(range(self.opt.n_epochs)):
                if epoch != 0:
                    for i, (data, labels) in enumerate(train_loader):
                        self.step = self.step + 1

                        # -----------------
                        #  Train Generator
                        # -----------------

                        optimG.zero_grad()

                        w_t = netG.state_dict()
                        gradients, generator_loss = unrolled_optimizer(w_t, w_star)

                        loss_student.append(generator_loss.item())

                        with torch.no_grad():
                            for p, g in zip(netG.parameters(), gradients):
                                p.grad = g

                        optimG.step()

                        print("{}/{}".format(i, len(train_loader)))

            res_student = []
            a_student = []
            b_student = []
            w_diff_student = []

            self.student.load_state_dict(torch.load('teacher_w0.pth'))

            generated_samples = np.zeros(2)
            for idx in tqdm(range(self.opt.n_iter)):
                if idx != 0:
                    w_t = self.student.lin.weight
                    w_t = w_t / torch.norm(w_t)

                    i = torch.randint(0, nb_batch, size=(1,)).item()
                    i_min = i * self.opt.batch_size
                    i_max = (i + 1) * self.opt.batch_size

                    gt_x = X_train[i_min:i_max].to(self.device)
                    y = Y_train[i_min:i_max].to(self.device)

                    # z = Variable(torch.cuda.FloatTensor(np.random.normal(0, 1, gt_x.shape)))
                    noise = Variable(torch.randn((self.opt.batch_size, self.opt.latent_dim))).to(self.device)

                    # x = torch.cat((w_t, w_t-w_star, gt_x, y.unsqueeze(0)), dim=1)
                    x = torch.cat((w_t, w_t-w_star, gt_x), dim=1)
                    # generated_sample = netG(x, y)

                    z, qz_mu, qz_std = netG(x, y)
                    generated_sample, x_mu, x_std, y_logit = vae.p_xy(z)

                    if idx == 1:
                        generated_samples = generated_sample.cpu().detach().numpy()  # [np.newaxis, :]
                        generated_labels = y.unsqueeze(1).cpu().detach().numpy()  # [np.newaxis, :]
                    else:
                        generated_samples = np.concatenate((generated_samples, generated_sample.cpu().detach().numpy()), axis=0)
                        generated_labels = np.concatenate((generated_labels, y.unsqueeze(1).cpu().detach().numpy()), axis=0)

                    # generated_sample = generated_sample @ proj_matrix.to(self.device)
                    self.student.update(generated_sample.detach(), y.unsqueeze(1))

                self.student.eval()
                test = self.student(X_test.to(self.device)).cpu()

                a, b = plot_classifier(self.student, X_test[:, 0].max(axis=0), X_test[:, 0].min(axis=0))
                a_student.append(a)
                b_student.append(b)

                if self.opt.data_mode == "mnist" or self.opt.data_mode == "gaussian" or self.opt.data_mode == "moon":
                    tmp = torch.where(test > 0.5, torch.ones(1), torch.zeros(1))
                    nb_correct = torch.where(tmp.view(-1) == Y_test, torch.ones(1), torch.zeros(1)).sum().item()
                elif self.opt.data_mode == "cifar10":
                    tmp = torch.max(test, dim=1).indices
                    nb_correct = torch.where(tmp == Y_test, torch.ones(1), torch.zeros(1)).sum().item()
                else:
                    sys.exit()
                acc = nb_correct / X_test.size(0)
                res_student.append(acc)

                w = self.student.lin.weight
                w = w / torch.norm(w)
                diff = torch.linalg.norm(w_star - w, ord=2) ** 2
                w_diff_student.append(diff.detach().clone().cpu())

                with open(logname, 'a') as logfile:
                    logwriter = csv.writer(logfile, delimiter=',')
                    logwriter.writerow([idx, acc, diff.item()])

            if self.opt.data_mode == "gaussian" or self.opt.data_mode == "moon":
                # make_results_img_2d(self.opt, X, Y, generated_samples, generated_labels, res_sgd, res_baseline, res_student, w_diff_sgd, w_diff_baseline, w_diff_student, 0, self.opt.seed)
                # make_results_video_2d(self.opt, X, Y, generated_samples, generated_labels, res_sgd, res_baseline, res_student, w_diff_sgd, w_diff_baseline, w_diff_student, epoch, self.opt.seed)

                a_star, b_star = plot_classifier(self.teacher, X_test[:, 0].max(axis=0), X_test[:, 0].min(axis=0))
                plot_generated_samples_2d(self.opt, X, Y, a_star, b_star, a_student, b_student, generated_samples, generated_labels, epoch, self.opt.seed)

                plot_distribution(self.opt, X_train, Y_train, generated_samples, generated_labels)
            else:
                make_results_img(self.opt, X, Y, generated_samples, generated_labels, res_sgd, res_baseline, res_student, w_diff_sgd, w_diff_baseline, w_diff_student, 0, self.opt.seed, proj_matrix)
                # make_results_video(self.opt, X, Y, generated_samples, generated_labels, res_sgd, res_baseline, res_student, w_diff_sgd, w_diff_baseline, w_diff_student, epoch, self.opt.seed, proj_matrix)

            save_folder = os.path.join(self.opt.log_path, "models", "weights_{}".format(epoch))
            if not os.path.exists(save_folder):
                os.makedirs(save_folder)

            save_path = os.path.join(save_folder, "netG_{}.pth".format("models", epoch))
            to_save = netG.state_dict()
            torch.save(to_save, save_path)

            # self.make_results_video_generated_data(generated_samples, epoch)