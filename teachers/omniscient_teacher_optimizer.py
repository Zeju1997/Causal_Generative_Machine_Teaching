# Some code is taken from the following Github repository:
# https://github.com/Ipsedo/IterativeMachineTeaching


from teachers.utils import BaseLinear, BaseConv
import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np


def clip_gradient(optimizer, grad_clip):
    """
    Clips gradients computed during backpropagation to avoid explosion of gradients.

    :param optimizer: optimizer with the gradients to be clipped
    :param grad_clip: clip value
    """
    for group in optimizer.param_groups:
        for param in group["params"]:
            if param.grad is not None:
                param.grad.data.clamp_(-grad_clip, grad_clip)


def __example_difficulty__(student, X, y):
    """
    Retourne la difficulté de l'exemple (X, y) selon le student
    :param student: Student ayant un attribut "lin" de class torch.nn.Linear
    :param X: La donnée
    :param y: Le label de la donnée
    :return: Le score de difficulté de l'exemple (X, y)
    """

    # We want to be able to calculate the gradient -> train()
    student.train()

    # Zeroing the accumulated gradient on the student's weights
    student.optim.zero_grad()

    # We want to retain the weight gradient of the linear layer lin
    # student.lin.weight.retain_grad()
    # X.requires_grad = True
    out = student(X)

    loss = student.loss_fn(out, y)
    loss.backward(retain_graph=True, create_graph=True)

    # test = grad(loss, X)

    # layer gradient recovery
    # res = student.lin.weight.grad
    # res_difficulty = Variable(student.lin.weight.grad, requires_grad=True)
    # res_difficulty = torch.clone(student.lin.weight.grad)
    res_difficulty = student.lin.weight.grad

    # res_difficulty.requires_grad = True

    example_difficulty_loss = torch.linalg.norm(res_difficulty, ord=2) ** 2
    # test = grad(example_difficulty_loss, X)# , create_graph=True)
    # example_difficulty_loss.backward()# create_graph=True, retain_graph=True)

    # returns the norm of the squared gradient
    # return (torch.linalg.norm(res, ord=2) ** 2).item()

    return example_difficulty_loss


def __example_usefulness__(student, w_star, X, y):
    """
    Retourne l'utilité de l'exemple (X, y) selon le student et les poids du teacher
    :param student: Student ayant un attribut "lin" de class torch.nn.Linear
    :param w_star: Les poids du teacher (hypothèse  objectif)
    :param X: La donnée
    :param y: Le label de la donnée
    :return: Le score d'utilité de l'exemple (X, y)
    """
    # différence des poids entre le student et le teacher
    diff = student.lin.weight - w_star

    # We want to be able to calculate the gradient -> train()
    student.train()

    # Zeroing the accumulated gradient on the student's weights
    student.optim.zero_grad()

    # We want to retain the weight gradient of the linear layer lin
    # student.lin.weight.retain_grad()

    out = student(X)

    loss = student.loss_fn(out, y)

    loss.backward(retain_graph=True, create_graph=True)

    # layer gradient recovery
    res = student.lin.weight.grad
    # res_useful = Variable(student.lin.weight.grad, requires_grad=True)

    example_usefulness_loss = torch.dot(diff.view(-1), res.view(-1))

    # produit scalaire entre la différence des poids et le gradient du student
    # return torch.dot(diff.view(-1), res.view(-1)).item()

    return example_usefulness_loss


def __get_weight_grad__(student, X, y):
    student.train()

    # Zeroing the accumulated gradient on the student's weights
    student.optim.zero_grad()

    # We want to retain the weight gradient of the linear layer lin
    # student.lin.weight.retain_grad()
    # X.requires_grad = True
    out = student(X)
    loss = student.loss_fn(out, y)
    loss.backward()

    res = student.lin.weight.grad
    return res


class ExampleDifficulty(nn.Module):
    def __init__(self, student, lr):
        super(ExampleDifficulty, self).__init__()
        self.lr = lr
        self.student = student

    def forward(self, input, target):
        return (self.lr ** 2) * self.student.example_difficulty(input, target)


class ExampleUsefulness(nn.Module):
    def __init__(self, student, teacher, lr):
        super(ExampleUsefulness, self).__init__()
        self.lr = lr
        self.student = student
        self.teacher = teacher

    def forward(self, input, target):
        return self.lr * 2 * self.student.example_usefulness(self.teacher.lin.weight, input, target)


class ScoreLoss(nn.Module):
    def __init__(self, example_difficulty, example_usefulness):
        super(ScoreLoss, self).__init__()
        self.example_usefulness = example_usefulness
        self.example_difficulty = example_difficulty

    def forward(self, input, target):
        # data = torch.Tensor(data).cuda()
        score_loss = self.example_difficulty(input, target) - self.example_usefulness(input, target)
        return score_loss.cpu().detach().numpy()


def approx_fprime(xk, f, epsilon, args=(), f0=None):
    """
    See ``approx_fprime``.  An optional initial function value arg is added.

    """
    if f0 is None:
        f0 = f(*((xk,) + args))
    grad = np.zeros((xk.shape[1],), float)
    # grad = torch.zeros(len(xk),).cuda()
    ei = np.zeros((xk.shape[1],), float)
    # ei = torch.zeros(len(xk),).cuda()
    for k in range(xk.shape[1]):
        ei[k] = 1.0
        d = epsilon * ei
        d = torch.Tensor(d).cuda()
        grad[k] = (f(*((xk + d,) + args)) - f0) / d[k]
        ei[k] = 0.0
    return grad


def fun(x, y):
    return x**2 + y


# objective function
def objective(x, y):
    return x**2.0 + y**2.0


# derivative of objective function
def derivative(x, y):
    return np.asarray([x * 2.0, y * 2.0])


def rosen(x):
    return (1-x[0])**2 + 105.*(x[1]-x[0]**2)**2


def __select_example__(teacher, student, opt, X, y, optimize_label=False):

    nb_example = X.size(0)
    nb_batch = int(nb_example / opt.batch_size)

    min_score = 1000
    arg_min = 0

    # TODO
    # - one "forward" scoring pass
    # - sort n * log(n)
    # - get first examples

    for i in range(nb_batch):
        i_min = i * opt.batch_size
        i_max = (i + 1) * opt.batch_size

        data = X[i_min:i_max]
        label = y[i_min:i_max]
        label = F.one_hot(label.long(), num_classes=2).type(torch.cuda.FloatTensor)

        lr = student.optim.param_groups[0]["lr"]

        # Calculate the score per batch
        s = (lr ** 2) * student.example_difficulty(data, label)
        s -= lr * 2 * student.example_usefulness(teacher.lin.weight, data, label)

        if s < min_score:
            min_score = s
            arg_min = i

    if optimize_label:

        alpha = opt.label_alpha
        beta1 = 0.8
        beta2 = 0.999
        # eps = 1e-8

        s_min = 1000
        count = 0

        i_min = arg_min * opt.batch_size
        i_max = (arg_min + 1) * opt.batch_size

        generated_sample = X[i_min:i_max]
        generated_label = y[i_min:i_max]

        # initialize first and second moments
        m = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
        v = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
        vhat = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]

        lr = student.optim.param_groups[0]["lr"]
        example_difficulty = ExampleDifficulty(student, lr)
        example_usefulness = ExampleUsefulness(student, teacher, lr)
        s1 = []
        x = []

        # generated_label = torch.randint(0, 2, (opt.batch_size,)).cuda()
        generated_label = F.one_hot(generated_label.long(), num_classes=2).type(torch.FloatTensor).cuda()
        generated_label.requires_grad = True

        constraints = [False] * opt.n_classes

        for t in range(opt.gd_n_label):
            generated_label.requires_grad = True

            loss = example_difficulty(generated_sample, generated_label) + example_usefulness(generated_sample, generated_label)
            # print("score loss", score_loss)

            grad = torch.autograd.grad(outputs=loss,
                                       inputs=generated_label,
                                       create_graph=False, retain_graph=False)

            grad = - grad[0].detach().squeeze(0)

            generated_label.requires_grad = False
            score = ScoreLoss(example_difficulty, example_usefulness)

            eps = np.sqrt(np.finfo(float).eps)
            # eps_list = [np.sqrt(200) * eps] * X.shape[1]
            # grad = approx_fprime(init_point, score_loss, eps_list)
            # grad = torch.Tensor(grad).cuda()

            # build a solution one variable at a time
            for i in range(opt.n_classes):
                if not constraints[i]:
                    if opt.optim == "adam":
                        # adam: convergence problem!
                        # m(t) = beta1 * m(t-1) + (1 - beta1) * g(t)
                        m[i] = beta1 * m[i] + (1.0 - beta1) * grad[i]
                        # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                        v[i] = beta2 * v[i] + (1.0 - beta2) * grad[i]**2
                        # mhat(t) = m(t) / (1 - beta1(t))
                        mhat = m[i] / (1.0 - beta1**(t+1))
                        # vhat(t) = v(t) / (1 - beta2(t))
                        vhat = v[i] / (1.0 - beta2**(t+1))
                        # x(t) = x(t-1) - alpha * mhat(t) / (sqrt(vhat(t)) + ep)
                        update = torch.Tensor([alpha]).cuda() * mhat / (torch.sqrt(vhat) + eps)

                    else:
                        # AMSGrad
                        # m(t) = beta1(t) * m(t-1) + (1 - beta1(t)) * g(t)
                        m[i] = beta1**(t+1) * m[i] + (1.0 - beta1**(t+1)) * grad[i]
                        # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                        v[i] = (beta2 * v[i]) + (1.0 - beta2) * grad[i]**2
                        # vhat(t) = max(vhat(t-1), v(t))
                        vhat[i] = max(vhat[i], v[i])
                        # x(t) = x(t-1) - alpha(t) * m(t) / sqrt(vhat(t)))
                        update = torch.Tensor([alpha]).cuda() * m[i] / (torch.sqrt(vhat[i]) + 1e-8)

                    # escape local minima?
                    #if torch.norm(grad) == 0:
                    #    noise = torch.empty(1).normal_(mean=0, std=0.1).cuda()
                    #    update = update + noise

                    if constraints[i]:
                        update[0] = 0

                    generated_label[0, i] = generated_label[0, i] - update

                    if generated_label[0, i] < 0:
                        constraints[i] = True
                        generated_label[0, i] = 0

            if torch.norm(generated_label, p=2) > opt.label_norm:
                generated_label = generated_label / torch.norm(generated_label) * opt.label_norm
            # print(generated_label)
            s = score(generated_sample, generated_label)

            x.append(s)

            if len(s1) != 0:
                if s == s1[-1]:
                    count = count + 1
                else:
                    count = 0

            if count > 10:
                break

            s1.append(s)

        # fig = plt.figure()
        # plt.plot(x, c="b", label="Teacher (CNN)")
        # plt.xlabel("Epoch")
        # plt.ylabel("Accuracy")
        # plt.legend()
        # plt.show()

    else:
        return arg_min

    return generated_sample, generated_label


def __generate_example__(teacher, opt, student, X, Y, optimize_label):
    """
    Selectionne un exemple selon le teacher et le student
    :param teacher: Le teacher de classe mère BaseLinear
    :param student: Le student devant implémenter les deux méthodes example_difficulty et example_usefulness
    :param X: Les données
    :param y: les labels des données
    :param batch_size: La taille d'un batch de données
    :return: L'indice de l'exemple à enseigner au student
    """

    nb_example = X.size(0)
    nb_batch = int(nb_example / opt.batch_size)

    s = 1000

    best_score = 1000
    count = 0

    alpha = 0.02 # alpha = 0.02
    beta1 = 0.8
    beta2 = 0.999
    # eps = 1e-8

    bounds = [[X.min().cpu(), X.max().cpu()]] * X.shape[1]
    bounds = np.asarray(bounds)
    # bounds = np.asarray([[X.min().cpu(), X.max().cpu()], [X.min().cpu(), X.max().cpu()]])
    constraints = [False] * bounds.shape[0]

    # generate an initial point
    # data_new1 = torch.rand(batch_size, X.size(1)).cuda() * 4 - 2
    label_new = torch.randint(0, 2, (opt.batch_size,)).cuda()
    label_new = F.one_hot(label_new, num_classes=2).type(torch.FloatTensor).cuda()
    # run the gradient descent updates

    s_min = 1000
    count = 0
    zz = []
    xx = []
    yy = []
    data_trajectory = []
    generated_sample = torch.zeros(opt.batch_size, X.shape[1]).cuda()
    generated_sample.requires_grad = True

    # init_point = torch.ones(batch_size, X.shape[1]).cuda() * X.min()
    # init_point = X.mean(dim=0).unsqueeze(0)

    diff = X.max(dim=0).values - X.min(dim=0).values

    # init_point = (X.max() - X.min()) * torch.rand(batch_size, X.size(1)).cuda() + X.min()
    # init_point = (X.max(dim=0).values - X.min(dim=0).values) * torch.rand(batch_size, X.size(1)).cuda() + X.min(dim=0).values

    # initialize first and second moments
    m = [torch.zeros(1).cuda() for _ in range(bounds.shape[0])]
    v = [torch.zeros(1).cuda() for _ in range(bounds.shape[0])]
    vhat = [torch.zeros(1).cuda() for _ in range(bounds.shape[0])]

    lr = student.optim.param_groups[0]["lr"]
    example_difficulty = ExampleDifficulty(student, lr)
    example_usefulness = ExampleUsefulness(student, teacher, lr)
    s1 = []

    x = []

    for t in range(opt.gd_n):
        generated_sample.requires_grad = True

        loss = example_difficulty(generated_sample, label_new) + example_usefulness(generated_sample, label_new)
        # print("score loss", score_loss)

        grad = torch.autograd.grad(outputs=loss,
                                   inputs=generated_sample,
                                   create_graph=False, retain_graph=False)

        grad = - grad[0].detach().squeeze(0)

        generated_sample.requires_grad = False
        score = ScoreLoss(example_difficulty, example_usefulness)

        eps = np.sqrt(np.finfo(float).eps)
        # eps_list = [np.sqrt(200) * eps] * X.shape[1]
        # grad = approx_fprime(init_point, score_loss, eps_list)
        # grad = torch.Tensor(grad).cuda()

        # build a solution one variable at a time
        for i in range(bounds.shape[0]):
            if not constraints[i]:
                if opt.optim == "adam":
                    # adam: convergence problem!
                    # m(t) = beta1 * m(t-1) + (1 - beta1) * g(t)
                    m[i] = beta1 * m[i] + (1.0 - beta1) * grad[i]
                    # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                    v[i] = beta2 * v[i] + (1.0 - beta2) * grad[i]**2
                    # mhat(t) = m(t) / (1 - beta1(t))
                    mhat = m[i] / (1.0 - beta1**(t+1))
                    # vhat(t) = v(t) / (1 - beta2(t))
                    vhat = v[i] / (1.0 - beta2**(t+1))
                    # x(t) = x(t-1) - alpha * mhat(t) / (sqrt(vhat(t)) + ep)
                    update = torch.Tensor([alpha]).cuda() * mhat / (torch.sqrt(vhat) + eps)

                else:
                    # AMSGrad
                    # m(t) = beta1(t) * m(t-1) + (1 - beta1(t)) * g(t)
                    m[i] = beta1**(t+1) * m[i] + (1.0 - beta1**(t+1)) * grad[i]
                    # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                    v[i] = (beta2 * v[i]) + (1.0 - beta2) * grad[i]**2
                    # vhat(t) = max(vhat(t-1), v(t))
                    vhat[i] = max(vhat[i], v[i])
                    # x(t) = x(t-1) - alpha(t) * m(t) / sqrt(vhat(t)))
                    update = torch.Tensor([alpha]).cuda() * m[i] / (torch.sqrt(vhat[i]) + 1e-8)

                # escape local minima?
                #if torch.norm(grad) == 0:
                #    noise = torch.empty(1).normal_(mean=0, std=0.1).cuda()
                #    update = update + noise

                if constraints[i]:
                    update[0] = 0

                generated_sample[0, i] = generated_sample[0, i] - update

                if generated_sample[0, i] > X.max() or generated_sample[0, i] < X.min():
                    constraints[i] = True

        s = score(generated_sample, label_new)

        x.append(s)

        if len(s1) != 0:
            if s == s1[-1]:
                count = count + 1
            else:
                count = 0

        if count > 10:
            break

        s1.append(s)

        zz.append(s)
        xx.append(generated_sample[0, 0].cpu())
        yy.append(generated_sample[0, 1].cpu())

        data_trajectory.append(generated_sample)

    # s1_np = np.array(s1)
    # idx = np.argmin(s1_np)
    # generated_sample = data_trajectory[idx]

    # x = []

    if optimize_label:
        alpha = opt.label_alpha

        # generated_label = torch.randint(0, 2, (opt.batch_size,)).cuda()
        # generated_label = F.one_hot(generated_label, num_classes=2).type(torch.FloatTensor).cuda()
        generated_label = label_new
        generated_label.requires_grad = True

        constraints = [False] * opt.n_classes

        m = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
        v = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
        vhat = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]

        s1 = []
        count = 0

        for t in range(opt.gd_n_label):
            generated_label.requires_grad = True

            loss = example_difficulty(generated_sample, generated_label) + example_usefulness(generated_sample, generated_label)
            # print("score loss", score_loss)

            grad = torch.autograd.grad(outputs=loss,
                                       inputs=generated_label,
                                       create_graph=False, retain_graph=False)

            grad = - grad[0].detach().squeeze(0)

            generated_label.requires_grad = False
            score = ScoreLoss(example_difficulty, example_usefulness)

            eps = np.sqrt(np.finfo(float).eps)
            # eps_list = [np.sqrt(200) * eps] * X.shape[1]
            # grad = approx_fprime(init_point, score_loss, eps_list)
            # grad = torch.Tensor(grad).cuda()

            # build a solution one variable at a time
            for i in range(opt.n_classes):
                if not constraints[i]:
                    if opt.optim == "adam":
                        # adam: convergence problem!
                        # m(t) = beta1 * m(t-1) + (1 - beta1) * g(t)
                        m[i] = beta1 * m[i] + (1.0 - beta1) * grad[i]
                        # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                        v[i] = beta2 * v[i] + (1.0 - beta2) * grad[i]**2
                        # mhat(t) = m(t) / (1 - beta1(t))
                        mhat = m[i] / (1.0 - beta1**(t+1))
                        # vhat(t) = v(t) / (1 - beta2(t))
                        vhat = v[i] / (1.0 - beta2**(t+1))
                        # x(t) = x(t-1) - alpha * mhat(t) / (sqrt(vhat(t)) + ep)
                        update = torch.Tensor([alpha]).cuda() * mhat / (torch.sqrt(vhat) + eps)

                    else:
                        # AMSGrad
                        # m(t) = beta1(t) * m(t-1) + (1 - beta1(t)) * g(t)
                        m[i] = beta1**(t+1) * m[i] + (1.0 - beta1**(t+1)) * grad[i]
                        # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                        v[i] = (beta2 * v[i]) + (1.0 - beta2) * grad[i]**2
                        # vhat(t) = max(vhat(t-1), v(t))
                        vhat[i] = max(vhat[i], v[i])
                        # x(t) = x(t-1) - alpha(t) * m(t) / sqrt(vhat(t)))
                        update = torch.Tensor([alpha]).cuda() * m[i] / (torch.sqrt(vhat[i]) + 1e-8)

                    # escape local minima?
                    #if torch.norm(grad) == 0:
                    #    noise = torch.empty(1).normal_(mean=0, std=0.1).cuda()
                    #    update = update + noise

                    if constraints[i]:
                        update[0] = 0

                    generated_label[0, i] = generated_label[0, i] - update

                    if generated_label[0, i] < 0:
                        constraints[i] = True
                        generated_label[0, i] = 0

            if torch.norm(generated_label, p=2) > opt.label_norm:
                generated_label = generated_label / torch.norm(generated_label) * opt.label_norm
            s = score(generated_sample, generated_label)

            x.append(s)

            if len(s1) != 0:
                if s - s1[-1] == 0:
                    count = count + 1
                else:
                    count = 0

            if count > 10:
                break

            s1.append(s)

        # fig = plt.figure()
        # plt.plot(x, c="b", label="Teacher (CNN)")
        # plt.xlabel("Epoch")
        # plt.ylabel("Accuracy")
        # plt.legend()
        # plt.show()

    else:
        generated_label = label_new

    return generated_sample, generated_label


def __generate_label__(teacher, opt, student, X, Y):
    """
    Selectionne un exemple selon le teacher et le student
    :param teacher: Le teacher de classe mère BaseLinear
    :param student: Le student devant implémenter les deux méthodes example_difficulty et example_usefulness
    :param X: Les données
    :param y: les labels des données
    :param batch_size: La taille d'un batch de données
    :return: L'indice de l'exemple à enseigner au student
    """

    nb_example = X.size(0)
    nb_batch = int(nb_example / opt.batch_size)

    s = 1000

    best_score = 1000
    count = 0

    alpha = opt.label_alpha
    beta1 = 0.8
    beta2 = 0.999
    # eps = 1e-8

    i = torch.randint(0, nb_batch, size=(1,)).item()
    i_min = i * opt.batch_size
    i_max = (i + 1) * opt.batch_size

    gt_x = X[i_min:i_max].cuda()
    generated_sample = gt_x

    # generated_label = torch.ones(opt.batch_size, 1).cuda() * 0.5
    generated_label = torch.randint(0, 2, (opt.batch_size,)).cuda()
    generated_label = F.one_hot(generated_label, num_classes=2).type(torch.FloatTensor).cuda()
    generated_label.requires_grad = True

    bounds = [[0, 1]]
    bounds = np.asarray(bounds)
    # bounds = np.asarray([[X.min().cpu(), X.max().cpu()], [X.min().cpu(), X.max().cpu()]])
    constraints = [False] * opt.n_classes

    m = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
    v = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]
    vhat = [torch.zeros(1).cuda() for _ in range(opt.n_classes)]

    lr = student.optim.param_groups[0]["lr"]
    example_difficulty = ExampleDifficulty(student, lr)
    example_usefulness = ExampleUsefulness(student, teacher, lr)
    s1 = []

    x = []

    for t in range(opt.gd_n_label):
        generated_label.requires_grad = True

        loss = example_difficulty(generated_sample, generated_label) + example_usefulness(generated_sample, generated_label)
        # print("score loss", score_loss)

        grad = torch.autograd.grad(outputs=loss,
                                   inputs=generated_label,
                                   create_graph=False, retain_graph=False)

        grad = - grad[0].detach().squeeze(0)

        generated_label.requires_grad = False
        score = ScoreLoss(example_difficulty, example_usefulness)

        eps = np.sqrt(np.finfo(float).eps)
        # eps_list = [np.sqrt(200) * eps] * X.shape[1]
        # grad = approx_fprime(init_point, score_loss, eps_list)
        # grad = torch.Tensor(grad).cuda()

        # build a solution one variable at a time
        for i in range(opt.n_classes):
            if not constraints[i]:
                if opt.optim == "adam":
                    # adam: convergence problem!
                    # m(t) = beta1 * m(t-1) + (1 - beta1) * g(t)
                    m[i] = beta1 * m[i] + (1.0 - beta1) * grad[i]
                    # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                    v[i] = beta2 * v[i] + (1.0 - beta2) * grad[i]**2
                    # mhat(t) = m(t) / (1 - beta1(t))
                    mhat = m[i] / (1.0 - beta1**(t+1))
                    # vhat(t) = v(t) / (1 - beta2(t))
                    vhat = v[i] / (1.0 - beta2**(t+1))
                    # x(t) = x(t-1) - alpha * mhat(t) / (sqrt(vhat(t)) + ep)
                    update = torch.Tensor([alpha]).cuda() * mhat / (torch.sqrt(vhat) + eps)

                else:
                    # AMSGrad
                    # m(t) = beta1(t) * m(t-1) + (1 - beta1(t)) * g(t)
                    m[i] = beta1**(t+1) * m[i] + (1.0 - beta1**(t+1)) * grad[i]
                    # v(t) = beta2 * v(t-1) + (1 - beta2) * g(t)^2
                    v[i] = (beta2 * v[i]) + (1.0 - beta2) * grad[i]**2
                    # vhat(t) = max(vhat(t-1), v(t))
                    vhat[i] = max(vhat[i], v[i])
                    # x(t) = x(t-1) - alpha(t) * m(t) / sqrt(vhat(t)))
                    update = torch.Tensor([alpha]).cuda() * m[i] / (torch.sqrt(vhat[i]) + 1e-8)

                # escape local minima?
                #if torch.norm(grad) == 0:
                #    noise = torch.empty(1).normal_(mean=0, std=0.1).cuda()
                #    update = update + noise

                if constraints[i]:
                    update[0] = 0

                generated_label[0, i] = generated_label[0, i] - update

                if generated_label[0, i] < 0:
                    constraints[i] = True

        if torch.norm(generated_label, p=2) > opt.label_norm:
            generated_label = generated_label / torch.norm(generated_label) * opt.label_norm
        # print("generated_label", generated_label)
        s = score(generated_sample, generated_label)

        x.append(s)

        if len(s1) != 0:
            if s == s1[-1]:
                count = count + 1
            else:
                count = 0

        s1.append(s)

        if count > 10:
            break

    return generated_sample, generated_label


class OmniscientLinearStudent(BaseLinear):
    """
    Classe pour le student du omniscient teacher
    Classification linéaire
    Marche de paire avec OmniscientLinearTeacher
    """
    def example_difficulty(self, X, y):
        return __example_difficulty__(self, X, y)

    def example_usefulness(self, w_star, X, y):
        return __example_usefulness__(self, w_star, X, y)

    def get_weight_grad(self, X, y):
        return __get_weight_grad__(self, X, y)


class OmniscientConvStudent(BaseConv):
    """
    Classe pour le student du omniscient teacher
    Modèle à convolution.
    Marche de paire avec OmniscientConvTeacher
    """
    def example_difficulty(self, X, y):
        return __example_difficulty__(self, X, y)

    def example_usefulness(self, w_star, X, y):
        return __example_usefulness__(self, w_star, X, y)


class OmniscientLinearTeacher(BaseLinear):
    """
    Omniscient teacher.
    Pour un classifieur linéaire de classe OmniscientLinearStudent
    """
    def generate_example(self, opt, student, X, y, optimize_label):
        return __generate_example__(self, opt, student, X, y, optimize_label)

    def generate_label(self, opt, student, X, y):
        return __generate_label__(self, opt, student, X, y)

    def select_example(self, student, opt, X, y, optimize_label=False):
        return __select_example__(self, student, opt, X, y, optimize_label=optimize_label)


class OmniscientConvTeacher(BaseConv):
    """
    Omnsicient teacher
    Pour un classifieur à convolution de classe OmniscientConvStudent
    """
    def select_example(self, student, X, y, batch_size):
        return __generate_example__(self, student, X, y, batch_size)
